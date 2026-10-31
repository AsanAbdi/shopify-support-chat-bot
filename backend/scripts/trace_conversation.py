"""Trace a short customer conversation through the REAL graph and print everything that happens inside.

Read-only: nothing is written to the database (the agent's checkpoints live in memory), nothing is sent to Freshdesk.

  free  (fake model, real embeddings):  docker compose run --rm --no-deps -T -e LLM_PROVIDER=fake api python - < backend/scripts/trace_conversation.py
  paid  (the model from .env):          docker compose run --rm --no-deps -T api python - < backend/scripts/trace_conversation.py

The paid run costs a few cents: two customer messages, three or four model calls.
"""

import asyncio
import json
import time
import traceback
import uuid

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import GraphDeps, build_graph
from app.agent.llm import build_chat_model
from app.agent.prompts import canary
from app.agent.sources import as_shown
from app.agent.text import message_text
from app.agent.tools import TOOL_SCHEMAS, ToolContext
from app.config import get_settings
from app.db import session_factory
from app.integrations.freshdesk import FreshdeskClient
from app.integrations.shopify import ShopifyClient
from app.rag.embeddings import get_embedder
from app.services.answer_stream import AnswerStreamer
from app.services.chat import AgentTurn

CONVERSATION = [
    "Hi! Do you have any discount codes or promotions I could use right now? I'm a first-time customer.",
    "Thanks! And where is my order 1002? My email is jan.devries@example.nl",
]


def print_tool_schemas() -> None:
    """The tool descriptions the model receives: `bind_tools` builds them from our pydantic classes and their docstrings."""
    try:
        from langchain_anthropic.chat_models import convert_to_anthropic_tool as convert
    except ImportError:
        from langchain_core.utils.function_calling import convert_to_openai_tool as convert
    print("\nTOOLS offered to the model on every call (built by bind_tools from our pydantic classes):")
    for schema in TOOL_SCHEMAS:
        print("  -", json.dumps(convert(schema), ensure_ascii=False))


def short(value, limit: int = 160) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def describe_message(m) -> str:
    calls = [(c["name"], c["args"]) for c in getattr(m, "tool_calls", None) or []]
    name = f" [{m.name}]" if getattr(m, "name", None) else ""
    return f"{type(m).__name__}{name}: {short(message_text(m), 180)}" + (f"  tool_calls={calls}" if calls else "")


def describe_value(value) -> str:
    if isinstance(value, list) and value and hasattr(value[0], "content"):
        return "[" + "; ".join(describe_message(m) for m in value) + "]"
    return short(value, 220)


class ModelTrace(AsyncCallbackHandler):
    """Records every model call: which node made it, what the model received, what it answered, the tokens."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._open: dict = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):  # noqa: ARG002
        tools = (kwargs.get("invocation_params") or {}).get("tools") or []
        self._open[run_id] = {"node": (metadata or {}).get("langgraph_node", "?"), "started": time.monotonic(),
                              "messages": messages[0], "tools": tools}

    async def on_llm_end(self, response, *, run_id, **kwargs):  # noqa: ARG002
        call = self._open.pop(run_id, None)
        if call is None:
            return
        message = response.generations[0][0].message
        call.update(seconds=time.monotonic() - call["started"], output=message,
                    usage=getattr(message, "usage_metadata", None) or {})
        self.calls.append(call)


def print_model_calls(tracer: ModelTrace, first_call_number: int) -> None:
    print("\n  [model calls in this turn]")
    for i, call in enumerate(tracer.calls, first_call_number):
        system = next((m for m in call["messages"] if type(m).__name__ == "SystemMessage"), None)
        rest = [m for m in call["messages"] if m is not system]
        print(f"  call #{i}: made by node '{call['node']}', {call['seconds']:.2f}s, tokens in/out = "
              f"{call['usage'].get('input_tokens', '?')}/{call['usage'].get('output_tokens', '?')}")
        names = [t.get("name") or t.get("function", {}).get("name") for t in call["tools"]]
        print(f"     tools offered to the model: {names}")
        if system is not None:
            text = message_text(system).replace(canary(), "CNRY-<masked>")
            print(f"     system prompt: {len(text)} chars; excerpts from the knowledge base included: "
                  f"{'Help-center excerpts' in text}")
            if i == first_call_number == 1:
                print("     --- system prompt, in full (the marker is masked) ---")
                print("\n".join("     | " + line for line in text.splitlines()))
                print("     --- end of system prompt ---")
        print("     conversation sent to the model:")
        for m in rest:
            print("       -", describe_message(m))
        print("     model answered:", describe_message(call["output"]))


def group_tokens(events: list[tuple[float, dict]]) -> list[str]:
    lines, tokens = [], []

    def flush() -> None:
        if tokens:
            text = "".join(t for _, t in tokens)
            lines.append(f"    +{tokens[0][0]:.2f}s..+{tokens[-1][0]:.2f}s  token x{len(tokens)}: {short(text, 300)!r}")
            tokens.clear()

    for dt, event in events:
        if event["type"] == "token":
            tokens.append((dt, event["text"]))
        else:
            flush()
            lines.append(f"    +{dt:.2f}s  {event['type']}")
    flush()
    return lines


async def trace_turn(graph, deps, thread: str, number: int, text: str, calls_so_far: int) -> list[dict]:
    tracer = ModelTrace()
    config = {"configurable": {"thread_id": thread, "deps": deps}, "callbacks": [tracer], "recursion_limit": 30}
    streamer = AnswerStreamer()
    started = time.monotonic()
    timeline: list[dict] = []
    events: list[tuple[float, dict]] = []

    print(f"\n{'=' * 100}\nCUSTOMER MESSAGE {number}: {text}\n{'=' * 100}")
    async for mode, payload in graph.astream({"messages": [HumanMessage(content=text)]}, config, stream_mode=["messages", "updates"]):
        now = time.monotonic() - started
        if mode == "messages":
            _, meta = payload
            node = meta.get("langgraph_node", "?")
            if timeline and timeline[-1]["kind"] == "chunks" and timeline[-1]["node"] == node:
                timeline[-1].update(count=timeline[-1]["count"] + 1, last=now)
            else:
                timeline.append({"kind": "chunks", "node": node, "count": 1, "first": now, "last": now})
        else:
            for node, update in (payload or {}).items():
                timeline.append({"kind": "update", "node": node, "at": now, "update": update or {}})
        events += [(now, e) for e in streamer.on_part(mode, payload)]
    events += [(time.monotonic() - started, e) for e in streamer.finish()]

    print("\n  [inside the graph, in order of arrival]  (what LangGraph streamed to our loop)")
    for item in timeline:
        if item["kind"] == "chunks":
            print(f"    +{item['first']:.2f}s..+{item['last']:.2f}s  model text chunks from node '{item['node']}': {item['count']}")
        else:
            print(f"    +{item['at']:.2f}s  node '{item['node']}' finished and wrote:")
            for key, value in item["update"].items():
                print(f"        {key} = {describe_value(value)}")

    print_model_calls(tracer, calls_so_far + 1)

    print("\n  [what the widget receives]  (events produced by AnswerStreamer)")
    print("\n".join(group_tokens(events)))
    state = (await graph.aget_state(config)).values
    turn = AgentTurn.from_state(state, question=text, lang=state.get("lang", "en"))
    print("    sources:", [as_shown(s, get_settings().public_base_url) for s in turn.cited] or "none")
    print(f"    done: escalated={bool(state.get('ticket_id'))}  no_answer={turn.no_answer}  total {time.monotonic() - started:.2f}s")
    print(f"\n  FINAL ANSWER:\n    {turn.answer}")
    return tracer.calls


def print_checkpoints(history: list) -> None:
    print(f"\n{'=' * 100}\nCHECKPOINTS of the whole conversation (oldest first)\n{'=' * 100}")
    for snap in history:
        v = snap.values
        print(f"  step={snap.metadata.get('step'):>3}  next={snap.next!s:20} msgs={len(v.get('messages', [])):<2} "
              f"tool_rounds={v.get('tool_rounds')} email={v.get('customer_email')} order={v.get('order_number')} "
              f"sources={len(v.get('turn_sources', []))} kb_context={len(v.get('kb_context') or '')} lang={v.get('lang')}")


async def main() -> None:
    s = get_settings()
    deps = GraphDeps(s, build_chat_model(s), build_chat_model(s, summary=True),
                     ToolContext(s, session_factory(), get_embedder(), ShopifyClient(s)), FreshdeskClient(s))
    graph = build_graph(InMemorySaver())
    thread = str(uuid.uuid4())
    print(f"model: {s.llm_provider} / {s.llm_model}   retrieval threshold: {s.retrieval_min_similarity}   shop API: {s.shopify_base_url}")
    print_tool_schemas()

    all_calls: list[dict] = []
    for number, text in enumerate(CONVERSATION, 1):
        try:
            all_calls += await trace_turn(graph, deps, thread, number, text, len(all_calls))
        except Exception:
            traceback.print_exc()
            break

    config = {"configurable": {"thread_id": thread, "deps": deps}}
    print_checkpoints([snap async for snap in graph.aget_state_history(config)][::-1])
    tokens_in = sum(c["usage"].get("input_tokens", 0) for c in all_calls)
    tokens_out = sum(c["usage"].get("output_tokens", 0) for c in all_calls)
    print(f"\nTOTAL: {len(all_calls)} model calls, {tokens_in} input tokens, {tokens_out} output tokens")


asyncio.run(main())
