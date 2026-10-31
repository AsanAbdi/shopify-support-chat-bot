"""LLM factory. Provider and model come from env (init_chat_model); `fake` is an offline rule-based model."""

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agent.guard import detect_lang, extract_email, extract_order_number, looks_like_injection
from app.agent.i18n import t
from app.agent.statuses import label
from app.agent.text import message_text
from app.config import Settings, get_settings


def build_chat_model(settings: Settings | None = None, *, summary: bool = False) -> BaseChatModel:
    s = settings or get_settings()
    if s.llm_provider == "fake":
        return FakeChatModel()
    model = s.summary_model if summary else s.llm_model
    if s.llm_provider == "bedrock":
        # Credentials come from the instance role / environment (boto3 chain); nothing secret is configured here.
        return init_chat_model(model, model_provider="bedrock_converse", region_name=s.aws_region,
                               temperature=s.llm_temperature, max_tokens=s.llm_max_tokens)
    key = s.anthropic_api_key if s.llm_provider == "anthropic" else s.openai_api_key
    if key is None:
        raise RuntimeError(f"LLM_PROVIDER={s.llm_provider} requires its API key in the environment")
    return init_chat_model(
        model,
        model_provider=s.llm_provider,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
        api_key=key.get_secret_value(),
        timeout=30,
        max_retries=2,
    )


# ----------------------------- fake model -----------------------------

_PRODUCT_NOUN = re.compile(
    r"\b(?:jackets?|shoes?|sneakers?|boots?|hoodies?|socks?|backpacks?|t-?shirts?|"
    r"jacke|jacken|schuhe?|stiefel|rucksack|rucksäcke|socken|"
    r"jas|jassen|schoenen|schoen|laarzen|rugzak|sokken)\b", re.I)
_PRODUCT_INTENT = re.compile(
    r"\b(?:price|cost|in stock|available|do you (?:have|sell)|looking for|recommend|under|"
    r"preis|kostet|lagernd|verfügbar|haben sie|suche|unter|"
    r"prijs|kost|voorraad|beschikbaar|heb je|hebben jullie|zoek|onder)\b", re.I)
_MAX_PRICE = re.compile(r"(?:under|below|max(?:imum)?|unter|bis|onder|tot)\s*(?:€|eur\s*)?(\d+)", re.I)


class FakeChatModel(BaseChatModel):
    """Deterministic stand-in so the whole stack runs (and is testable) without an LLM key.

    It routes to tools with simple rules and composes answers from tool output. It is NOT a quality model:
    switch to LLM_PROVIDER=anthropic for real behaviour.
    """

    @property
    def _llm_type(self) -> str:
        return "fake-rule-based"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        # Like real chat models, return a RunnableBinding (not self): this catches bugs in how callbacks/config are merged.
        return self.bind(tools_bound=len(list(tools)))

    # ---- decision logic ----
    def _respond(self, messages: list[BaseMessage]) -> AIMessage:
        system = next((message_text(m) for m in messages if isinstance(m, SystemMessage)), "")
        if "[task:summary]" in system:
            body = " | ".join(message_text(m)[:80] for m in messages if isinstance(m, HumanMessage))
            return AIMessage(content=f"Customer asked: {body}"[:500])
        if "[task:ticket_summary]" in system:
            humans = [message_text(m) for m in messages if isinstance(m, HumanMessage)]
            return AIMessage(content="\n".join(["Customer request (auto summary):", *[f"- {h[:140]}" for h in humans[-4:]]]))

        last_h = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
        question = message_text(messages[last_h]) if last_h >= 0 else ""
        lang = detect_lang(question)
        tool_msgs = [m for m in messages[last_h + 1 :] if isinstance(m, ToolMessage)]
        if tool_msgs:
            return AIMessage(content=self._compose(tool_msgs, lang, question))
        if looks_like_injection(question):
            return AIMessage(content=t("injection", lang))

        email, order = extract_email(question), extract_order_number(question)
        # earlier turns only fill in the missing half of an order lookup the customer is asking about right now
        wants_order = re.search(r"order|bestell|status|track|sendung|verzend|pakket|paket|package", question, re.I) or (email and order)
        if wants_order:
            history = " ".join(message_text(m) for m in messages[:last_h] if isinstance(m, HumanMessage))
            email, order = email or extract_email(history), order or extract_order_number(history)
        if wants_order and order:
            if not email:
                return AIMessage(content=t("order_ask_email", lang))
            return self._call("get_order_status", {"order_number": order, "email": email})
        if _PRODUCT_NOUN.search(question) and _PRODUCT_INTENT.search(question) and not re.search(r"return|refund|shipping|retour|rückgabe|versand|verzending", question, re.I):
            args: dict[str, Any] = {"query": question}
            if m := _MAX_PRICE.search(question):
                args["max_price"] = float(m.group(1))
            return self._call("search_products", args)
        if "Help-center excerpts" in system:  # fast path: the graph already retrieved confident excerpts
            return AIMessage(content=self._from_excerpts(system.split("Help-center excerpts", 1)[1].split("\n", 1)[1]))
        return self._call("search_knowledge_base", {"query": question})

    @staticmethod
    def _from_excerpts(content: str) -> str:
        best = content.strip().split("\n\n[", 1)[0]  # top-ranked passage
        n = re.match(r"\[(\d+)\]", best)
        body = best.split("\n", 2)[-1].strip()
        return f"{body[:420].rstrip()} [{n.group(1) if n else 1}]"

    @staticmethod
    def _call(name: str, args: dict) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "tool_call"}])

    @staticmethod
    def _compose(tool_msgs: list[ToolMessage], lang: str, question: str = "") -> str:
        tm = tool_msgs[-1]
        art = tm.artifact if isinstance(tm.artifact, dict) else {}
        kind = art.get("kind")
        if tm.name == "search_knowledge_base":
            content = message_text(tm)
            if "NO_RELEVANT_RESULTS" in content:
                return t("no_answer", lang)
            return FakeChatModel._from_excerpts(content)
        if kind == "order":
            if art.get("error"):
                return t("order_unavailable", lang)
            if not art.get("found"):
                return t("order_not_found", lang)
            out = t("order_status", lang, number=art["number"], status=label("fulfillment", art["status"], lang))
            for sh in art.get("shipments", []):
                if sh.get("number"):
                    out += t("order_shipped", lang, date=sh["shipped"], carrier=sh.get("carrier") or "-", tracking=sh["number"])
                if sh.get("delivered", "-") != "-":
                    out += t("order_delivered", lang, date=sh["delivered"])
                elif sh.get("estimated", "-") != "-":
                    out += t("order_eta", lang, date=sh["estimated"])
            return out
        if kind == "products":
            items = art.get("items", [])
            if not items:
                return t("products_none", lang)
            lines = [t("products_found", lang)]
            for it in items[:4]:
                stock = t("in_stock" if it["available"] else "out_of_stock", lang)
                lines.append(f"- {it['title']}: {it['price']:.2f} {it['currency']}, {stock} - {it['url']}")
            return "\n".join(lines)
        return message_text(tm)[:300]

    # ---- BaseChatModel plumbing ----
    @staticmethod
    def _usage(messages: list[BaseMessage], out: AIMessage) -> dict:
        i = sum(len(message_text(m)) for m in messages) // 4 + 1
        o = max(len(message_text(out)) // 4, 1) + 10 * len(out.tool_calls)
        return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o}

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:  # noqa: ARG002
        out = self._respond(messages)
        out.usage_metadata = self._usage(messages, out)  # type: ignore[assignment]
        out.response_metadata = {"model_name": "fake-rule-based"}
        return ChatResult(generations=[ChatGeneration(message=out)])

    def _stream(self, messages: list[BaseMessage], stop: list[str] | None = None,
                run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:  # noqa: ARG002
        out = self._respond(messages)
        text = message_text(out)
        words = re.findall(r"\S+\s*", text)
        for w in words:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=w))
            if run_manager:
                run_manager.on_llm_new_token(w, chunk=chunk)
            yield chunk
        usage = self._usage(messages, out)
        yield ChatGenerationChunk(message=AIMessageChunk(
            content="", tool_call_chunks=[
                {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                for i, tc in enumerate(out.tool_calls)
            ],
            usage_metadata=usage, response_metadata={"model_name": "fake-rule-based"}))

    async def _astream(self, messages: list[BaseMessage], stop: list[str] | None = None,
                       run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:  # noqa: ARG002
        for chunk in self._stream(messages):
            if run_manager and chunk.message.content:
                await run_manager.on_llm_new_token(str(chunk.message.content), chunk=chunk)
            yield chunk
