"""LangGraph agent: prepare -> (escalate | agent <-> tools) -> finalize. Checkpointed per conversation in Postgres."""

import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agent import escalation
from app.agent.guard import (
    detect_lang,
    escalation_trigger,
    extract_email,
    extract_order_number,
    looks_like_injection,
)
from app.agent.i18n import t
from app.agent.prompts import SUMMARY_SYSTEM, build_system_prompt, canary
from app.agent.state import AgentState
from app.agent.statuses import describe_now
from app.agent.text import message_text
from app.agent.tools import HANDLERS, TOOL_SCHEMAS, ToolContext, format_hits
from app.config import Settings
from app.rag.retriever import is_confident, retrieve

log = logging.getLogger(__name__)

LEAK_REFUSAL_KEY = "injection"


@dataclass
class GraphDeps:
    settings: Settings
    llm: BaseChatModel
    summary_llm: BaseChatModel
    tool_ctx: ToolContext
    freshdesk: Any


def _deps(config: RunnableConfig) -> GraphDeps:
    return config["configurable"]["deps"]


def _last_human(state: AgentState) -> str:
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            return message_text(m)
    return ""


def contains_canary(text: str) -> bool:
    return canary().lower() in text.lower()


# ------------------------------- nodes -------------------------------

async def _prefetch_kb(state: AgentState, text: str, deps: GraphDeps) -> dict:
    ctx = deps.tool_ctx
    query = text
    if len(text.split()) < 6:  # elliptical follow-up ("and the refund?"): add the previous customer message for context
        earlier = [message_text(m) for m in state["messages"][:-1] if isinstance(m, HumanMessage)]
        query = f"{earlier[-1]} {text}" if earlier else text
    try:
        async with ctx.session_factory() as session:
            hits = await retrieve(session, ctx.embedder, query[:500], settings=ctx.settings)
    except Exception:
        log.exception("knowledge-base prefetch failed; the model will use the tool")
        return {}
    if not is_confident(hits, ctx.settings):
        return {}
    context, sources = format_hits(hits, [], pre=True)
    return {"kb_context": context, "turn_sources": sources, "low_conf_streak": 0}


async def prepare(state: AgentState, config: RunnableConfig) -> dict:
    deps = _deps(config)
    s = deps.settings
    text = _last_human(state)
    update: dict[str, Any] = {
        "turn_sources": [], "turn_no_answer": False, "tool_rounds": 0,
        "injection": looks_like_injection(text),
        "lang": detect_lang(text, state.get("lang", "en")),
    }
    if email := extract_email(text):
        update["customer_email"] = email
    if (order := extract_order_number(text)) and not state.get("order_number"):
        update["order_number"] = order

    # Escalation triggers (F4): decided deterministically, before the LLM gets a chance to improvise.
    reason = escalation_trigger(text)
    if reason:
        update["pending_escalation"] = reason
    elif state.get("pending_escalation") and state.get("email_asked") and not extract_email(text):
        update["pending_escalation"] = None  # we asked for the email once; the customer moved on
        update["email_asked"] = False

    # Fast path: retrieve before the model runs. A confident hit goes straight into the prompt, so a FAQ question
    # costs one LLM call instead of two (tool decision + answer). Misses fall back to the model calling the tool,
    # which is also what feeds the "two misses in a row -> human" rule.
    pending = update["pending_escalation"] if "pending_escalation" in update else state.get("pending_escalation")
    update["kb_context"] = ""
    if not pending and not update["injection"] and len(text.strip()) >= 3:
        update.update(await _prefetch_kb(state, text, deps))

    # Long conversations are compressed into a summary to keep the context small.
    msgs = state["messages"]
    if len(msgs) > s.summary_trigger_messages:
        cut = len(msgs) - s.summary_keep_messages
        while cut < len(msgs) and not isinstance(msgs[cut], HumanMessage):
            cut += 1
        old = msgs[:cut]
        if old and cut < len(msgs):
            transcript = "\n".join(
                f"{'Customer' if isinstance(m, HumanMessage) else 'Bot'}: {message_text(m)}"
                for m in old if isinstance(m, HumanMessage | AIMessage) and message_text(m).strip()
            )
            prev = state.get("summary", "")
            res = await deps.summary_llm.ainvoke(
                [SystemMessage(content=SUMMARY_SYSTEM),
                 HumanMessage(content=(f"Previous summary: {prev}\n\n" if prev else "") + transcript[-10000:])],
                config={"callbacks": config.get("callbacks")},
            )
            update["summary"] = message_text(res).strip()
            update["messages"] = [RemoveMessage(id=m.id) for m in old if m.id]
    return update


def route_after_prepare(state: AgentState) -> str:
    return "escalate" if state.get("pending_escalation") else "agent"


async def agent(state: AgentState, config: RunnableConfig) -> dict:
    deps = _deps(config)
    prompt = build_system_prompt(
        state.get("summary", ""), injection_suspected=bool(state.get("injection")),
        pending_email=bool(state.get("pending_escalation")), kb_context=state.get("kb_context", ""),
        today=describe_now(deps.settings),
    )
    can_use_tools = state.get("tool_rounds", 0) < deps.settings.agent_max_tool_rounds
    llm = deps.llm.bind_tools(TOOL_SCHEMAS) if can_use_tools else deps.llm
    msg = await llm.ainvoke([SystemMessage(content=prompt), *state["messages"]], config)
    return {"messages": [msg]}


def route_after_agent(state: AgentState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else "finalize"


async def tools(state: AgentState, config: RunnableConfig) -> dict:
    deps = _deps(config)
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    working: AgentState = dict(state)  # type: ignore[assignment]  # sequential calls see earlier updates
    out_msgs: list[ToolMessage] = []
    merged: dict[str, Any] = {}
    for call in last.tool_calls:
        handler = HANDLERS.get(call["name"])
        if handler is None:
            out_msgs.append(ToolMessage(content="UNKNOWN_TOOL", tool_call_id=call["id"], name=call["name"]))
            continue
        try:
            result = await handler(call["args"], working, deps.tool_ctx)
        except Exception:
            log.exception("tool %s failed", call["name"])
            out_msgs.append(ToolMessage(content="TOOL_ERROR. Apologise briefly and offer a human colleague.", tool_call_id=call["id"], name=call["name"]))
            continue
        out_msgs.append(ToolMessage(content=result.content, tool_call_id=call["id"], name=call["name"], artifact=result.artifact))
        working.update(result.update)  # type: ignore[typeddict-item]
        merged.update(result.update)
    merged["tool_rounds"] = state.get("tool_rounds", 0) + 1
    if (
        not merged.get("pending_escalation") and not state.get("ticket_id")
        and working.get("low_conf_streak", 0) >= 2
    ):
        merged["pending_escalation"] = "low_confidence"
    return {"messages": out_msgs, **merged}


def route_after_tools(state: AgentState) -> str:
    return "escalate" if state.get("pending_escalation") else "agent"


async def escalate(state: AgentState, config: RunnableConfig) -> dict:
    deps = _deps(config)
    s, lang = deps.settings, state.get("lang", "en")
    reason = state.get("pending_escalation") or "other"
    if state.get("ticket_id"):
        msg = t("already_escalated", lang, ticket=state["ticket_id"], hours=s.escalation_sla_hours)
        return {"messages": [AIMessage(content=msg)], "pending_escalation": None}
    email = state.get("customer_email")
    if not email:
        return {"messages": [AIMessage(content=t("ask_email", lang))], "email_asked": True}
    result = await escalation.create_ticket(
        freshdesk=deps.freshdesk, llm=deps.summary_llm, settings=s, messages=state["messages"],
        conv_summary=state.get("summary", ""), lang=lang, email=email, order_number=state.get("order_number"), reason=reason,
    )
    if result.ticket_id is None:
        msg = t("handoff_failed", lang, support_email=s.support_email)
        return {"messages": [AIMessage(content=msg)], "pending_escalation": None, "email_asked": False}
    msg = t("handoff", lang, email=email, hours=s.escalation_sla_hours)
    return {
        "messages": [AIMessage(content=msg)], "ticket_id": result.ticket_id, "pending_escalation": None,
        "email_asked": False, "low_conf_streak": 0,
        "turn_sources": [{"n": 0, "type": "ticket", "ref": result.ticket_id, "reason": reason, "summary": result.summary}],
    }


async def finalize(state: AgentState, config: RunnableConfig) -> dict:  # noqa: ARG001
    """Output guard: never let the hidden system prompt leave the building."""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and contains_canary(message_text(last)):
        log.warning("system prompt leak blocked", extra={"event": "leak_blocked"})
        return {"messages": [AIMessage(content=t(LEAK_REFUSAL_KEY, state.get("lang", "en")), id=last.id)]}
    return {}


def build_graph(checkpointer) -> Any:
    g = StateGraph(AgentState)
    g.add_node("prepare", prepare)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("escalate", escalate)
    g.add_node("finalize", finalize)
    g.add_edge(START, "prepare")
    g.add_conditional_edges("prepare", route_after_prepare, {"escalate": "escalate", "agent": "agent"})
    g.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "finalize": "finalize"})
    g.add_conditional_edges("tools", route_after_tools, {"escalate": "escalate", "agent": "agent"})
    g.add_edge("escalate", END)
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
