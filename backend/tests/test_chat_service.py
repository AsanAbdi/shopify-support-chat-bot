"""ChatService at the service level (no HTTP): what one turn does to Redis, Postgres, metrics and the event stream.

Two kinds of tests:
 * the real graph with the fake model (`service`): the whole turn, end to end;
 * a scripted graph (`scripted`): replays exact LangGraph stream parts, to pin down how they become widget events.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, AIMessageChunk
from prometheus_client import REGISTRY
from sqlalchemy import select, text, update

import app.db as db
from app.agent.i18n import t
from app.agent.prompts import canary
from app.config import get_settings
from app.models import Conversation, LlmUsage, Message, UnansweredQuestion, _now
from app.services.chat import ChatService
from tests.helpers import make_harness

RETURNS_QUESTION = "How many days do I have to return an item?"
AGENT = {"langgraph_node": "agent"}


# ------------------------------- harness -------------------------------

@pytest.fixture(autouse=True)
def _app_db_points_at_the_test_database(engine, sm, monkeypatch):
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_sessionmaker", sm)


class ScriptedGraph:
    """Stands in for the compiled LangGraph: replays stream parts, then reports a prepared final state."""

    def __init__(self, parts: list, values: dict) -> None:
        self.parts, self.values = parts, values

    async def astream(self, inputs, config, stream_mode):  # noqa: ARG002
        for part in self.parts:
            yield part

    async def aget_state(self, config):  # noqa: ARG002
        return SimpleNamespace(values=self.values)


class ExplodingGraph:
    async def astream(self, inputs, config, stream_mode):  # noqa: ARG002
        raise RuntimeError("model provider is down")
        yield  # pragma: no cover - makes this an async generator


def _build(sm, redis, graph=None) -> ChatService:
    harness = make_harness(sm)
    container = SimpleNamespace(
        settings=get_settings(), redis=redis, graph=graph or harness.graph,
        graph_deps=harness.deps, langfuse_enabled=False,
    )
    return ChatService(container)


@pytest_asyncio.fixture
async def service(sm, redis, seeded_kb):
    return _build(sm, redis)


@pytest.fixture
def scripted(sm, redis):
    def make(parts: list, values: dict) -> ChatService:
        return _build(sm, redis, ScriptedGraph(parts, values))

    return make


def final_state(answer: str, **extra) -> dict:
    return {"messages": [AIMessage(content=answer)], "lang": "en", **extra}


def chunk(text_: str) -> tuple:
    return "messages", (AIMessageChunk(content=text_), AGENT)


async def run(service: ChatService, text_: str = "hello there", conv_id: str | None = None) -> list[dict]:
    return [e async for e in service.stream_turn(conv_id, text_, "iphash")]


def kinds(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def streamed_text(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "token")


def metric(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def rows(sm, model, *where, order=None):
    async with sm() as s:
        q = select(model).where(*where)
        if order is not None:
            q = q.order_by(order)
        return (await s.execute(q)).scalars().all()


async def conversation(sm, conv_id: str) -> Conversation:
    async with sm() as s:
        return await s.get(Conversation, conv_id)


async def new_conversation(sm, **fields) -> str:
    conv_id = str(uuid.uuid4())
    async with sm() as s:
        s.add(Conversation(id=conv_id, **fields))
        await s.commit()
    return conv_id


# ------------------------------- the whole turn -------------------------------

async def test_a_new_conversation_is_announced_streamed_and_fully_stored(service, sm):
    llm_calls_before = len(await rows(sm, LlmUsage))
    events = await run(service, RETURNS_QUESTION)

    assert kinds(events)[0] == "session" and events[0]["new"] is True
    assert kinds(events)[-1] == "done" and kinds(events).index("sources") < kinds(events).index("done")
    conv_id = events[0]["id"]
    done = events[-1]
    assert done["escalated"] is False and done["ticket_id"] is None and done["message_id"]

    user, assistant = await rows(sm, Message, Message.conversation_id == conv_id, order=Message.id)
    assert (user.role, user.content) == ("user", RETURNS_QUESTION)
    assert assistant.role == "assistant" and assistant.id == done["message_id"]
    assert assistant.content == streamed_text(events).strip() and not assistant.no_answer
    assert assistant.sources and assistant.sources[0]["url"].endswith("/file")

    usage = await rows(sm, LlmUsage, LlmUsage.conversation_id == conv_id)
    assert len(usage) > llm_calls_before and all(u.cost_eur == 0 for u in usage)  # the fake model is free
    conv = await conversation(sm, conv_id)
    assert conv.client_ip_hash == "iphash" and conv.lang == "en" and conv.last_activity_at


async def test_a_follow_up_in_the_same_conversation_is_not_announced_again(service, sm):
    first = await run(service, RETURNS_QUESTION)
    conv_id = first[0]["id"]
    second = await run(service, "And what about the refund?", conv_id)

    assert kinds(second)[0] != "session" and kinds(second)[-1] == "done"
    roles = [m.role for m in await rows(sm, Message, Message.conversation_id == conv_id, order=Message.id)]
    assert roles == ["user", "assistant", "user", "assistant"]


async def test_an_expired_conversation_is_replaced_by_a_new_one(service, sm):
    old_id = (await run(service, RETURNS_QUESTION))[0]["id"]
    async with sm() as s:
        await s.execute(update(Conversation).values(last_activity_at=_now() - timedelta(hours=25)))
        await s.commit()

    events = await run(service, RETURNS_QUESTION, old_id)

    assert events[0]["type"] == "session" and events[0]["id"] != old_id
    assert await rows(sm, Message, Message.conversation_id == old_id, order=Message.id)  # the old one is left alone


async def test_the_stored_message_is_trimmed(service, sm):
    events = await run(service, "   " + RETURNS_QUESTION + "  \n")
    user = (await rows(sm, Message, Message.role == "user"))[0]
    assert user.content == RETURNS_QUESTION and events[-1]["type"] == "done"


@pytest.mark.parametrize("bad", ["", "   \n", "x" * 2001])
async def test_invalid_messages_are_rejected_before_anything_is_stored(service, sm, redis, bad):
    assert len(bad.strip()) == 0 or len(bad) > get_settings().max_user_message_chars
    blocked_before = metric("chat_blocked_total", kind="bad_length")

    events = await run(service, bad)

    assert events == [{"type": "error", "code": "invalid_message", "text": t("error", "en")}]
    assert not await rows(sm, Conversation) and not await rows(sm, Message)
    assert not await redis.keys("chat-lock:*")
    assert metric("chat_blocked_total", kind="bad_length") == blocked_before + 1


async def test_a_busy_conversation_is_refused_and_the_other_turns_lock_survives(service, sm, redis):
    conv_id = await new_conversation(sm, lang="de")
    await redis.set(f"chat-lock:{conv_id}", "1", ex=60)  # another turn is still running
    busy_before = metric("chat_blocked_total", kind="busy")
    finished_before = metric("chat_turn_seconds_count")

    events = await run(service, RETURNS_QUESTION, conv_id)

    assert events == [{"type": "error", "code": "busy", "text": t("error", "de")}]
    assert not await rows(sm, Message)
    assert await redis.exists(f"chat-lock:{conv_id}") == 1  # not ours to release
    assert metric("chat_blocked_total", kind="busy") == busy_before + 1
    assert metric("chat_turn_seconds_count") == finished_before  # a refused message is not a turn


async def test_the_lock_is_held_while_the_turn_runs_and_released_afterwards(service, redis):
    seen_during_turn: list[tuple[bool, int]] = []
    conv_id = None
    async for event in service.stream_turn(None, RETURNS_QUESTION, "iphash"):
        if event["type"] == "session":
            conv_id = event["id"]
        elif not seen_during_turn:
            key = f"chat-lock:{conv_id}"
            seen_during_turn.append((await redis.exists(key) == 1, await redis.ttl(key)))

    held, ttl = seen_during_turn[0]
    assert held and 0 < ttl <= 120
    assert await redis.exists(f"chat-lock:{conv_id}") == 0


async def test_an_exhausted_budget_answers_without_running_the_agent(service, sm, redis):
    async with sm() as s:
        await s.execute(text("INSERT INTO llm_usage (model, input_tokens, output_tokens, cost_eur) VALUES ('x', 1, 1, 5000)"))
        await s.commit()
    budget_turns_before = metric("chat_turns_total", lang="en", outcome="budget")

    events = await run(service, RETURNS_QUESTION)

    message = t("budget", "en", support_email=get_settings().support_email)
    assert events[1:] == [{"type": "token", "text": message}, {"type": "done", "escalated": False}]
    conv_id = events[0]["id"]
    user, assistant = await rows(sm, Message, Message.conversation_id == conv_id, order=Message.id)
    assert (user.content, assistant.content, assistant.sources) == (RETURNS_QUESTION, message, [])
    assert len(await rows(sm, LlmUsage)) == 1  # only the row we inserted: the model was never called
    assert await redis.exists(f"chat-lock:{conv_id}") == 0
    assert metric("chat_turns_total", lang="en", outcome="budget") == budget_turns_before + 1


async def test_an_agent_failure_becomes_an_internal_error_and_releases_the_lock(sm, redis):
    service = _build(sm, redis, ExplodingGraph())
    failed_before = metric("chat_turns_total", lang="en", outcome="error")

    events = await run(service, "hello there")

    assert kinds(events) == ["session", "error"]
    assert events[1] == {"type": "error", "code": "internal", "text": t("error", "en")}
    conv_id = events[0]["id"]
    assert [m.role for m in await rows(sm, Message, Message.conversation_id == conv_id)] == ["user"]  # the question is kept
    assert await redis.exists(f"chat-lock:{conv_id}") == 0
    assert metric("chat_turns_total", lang="en", outcome="error") == failed_before + 1


async def test_a_handover_is_recorded_on_the_conversation(service, sm):
    escalations_before = metric("chat_escalations_total", reason="human_request")

    events = await run(service, "I want to speak to a human, my email is buyer@example.org")

    done = events[-1]
    assert done["type"] == "done" and done["escalated"] is True and done["ticket_id"]
    conv = await conversation(sm, events[0]["id"])
    assert conv.escalated and conv.ticket_id == done["ticket_id"]
    assert conv.escalation_reason == "human_request" and conv.ticket_summary
    assert conv.customer_email == "buyer@example.org"
    assert metric("chat_escalations_total", reason="human_request") == escalations_before + 1


async def test_an_unanswered_question_is_recorded_with_personal_data_masked(service, sm):
    no_answer_before = metric("chat_no_answer_total")

    events = await run(service, "What will the weather be like in Berlin tomorrow today? anna.schmidt@example.de")

    assert events[-1]["type"] == "done"
    assistant = (await rows(sm, Message, Message.role == "assistant"))[0]
    assert assistant.no_answer
    (question,) = await rows(sm, UnansweredQuestion)
    assert "anna.schmidt" not in question.question and "weather" in question.question
    assert (await conversation(sm, events[0]["id"])).had_no_answer
    assert metric("chat_no_answer_total") == no_answer_before + 1


# ------------------------------- stream parts -> widget events -------------------------------

async def test_text_written_before_a_tool_call_is_discarded_with_a_reset(scripted, sm):
    tool_call = AIMessage(content="", tool_calls=[{"name": "search_knowledge_base", "args": {"query": "x"}, "id": "c1"}])
    parts = [
        chunk("P" * 40),  # a preamble: long enough to get past the leak guard's hold-back window
        ("updates", {"agent": {"messages": [tool_call]}}),
        chunk("F" * 40),
    ]
    events = await run(scripted(parts, final_state("F" * 40)))

    assert kinds(events) == ["session", "token", "reset", "token", "token", "done"]
    assert streamed_text(events[3:]) == "F" * 40
    assert (await rows(sm, Message, Message.role == "assistant"))[0].content == "F" * 40


async def test_an_answer_that_differs_from_the_stream_is_corrected_with_a_replace(scripted):
    events = await run(scripted([chunk("A" * 40)], final_state("B" * 10)))

    assert kinds(events) == ["session", "token", "token", "replace", "done"]
    assert events[3]["text"] == "B" * 10


async def test_an_identical_stream_needs_no_replace_even_with_stray_whitespace(scripted):
    events = await run(scripted([chunk("C" * 40 + "  ")], final_state("C" * 40)))
    assert "replace" not in kinds(events)


async def test_the_handover_message_is_sent_as_one_token_without_a_first_token_measurement(scripted):
    parts = [("updates", {"escalate": {"messages": [AIMessage(content="Please tell me your email address.")]}})]
    first_tokens_before = metric("chat_first_token_seconds_count")

    events = await run(scripted(parts, final_state("Please tell me your email address.")))

    assert events[1:-1] == [{"type": "token", "text": "Please tell me your email address."}]
    assert metric("chat_first_token_seconds_count") == first_tokens_before  # only model tokens are timed


async def test_streamed_model_text_is_timed_once(scripted):
    first_tokens_before = metric("chat_first_token_seconds_count")
    await run(scripted([chunk("A" * 40), chunk("A" * 40)], final_state("A" * 80)))
    assert metric("chat_first_token_seconds_count") == first_tokens_before + 1


async def test_only_text_from_the_agent_node_reaches_the_customer(scripted):
    blocks = AIMessageChunk(content=[{"type": "text", "text": "X" * 40}, {"type": "tool_use", "id": "t", "name": "n", "input": {}}])
    parts = [
        ("messages", (AIMessageChunk(content="SECRET" * 10), {"langgraph_node": "tools"})),  # another node
        ("messages", (AIMessage(content="SECRET" * 10), AGENT)),  # not a streaming chunk
        ("messages", (blocks, AGENT)),  # provider-style content blocks: only the text blocks count
    ]
    events = await run(scripted(parts, final_state("X" * 40)))

    assert streamed_text(events) == "X" * 40 and "replace" not in kinds(events)


async def test_a_canary_split_over_chunks_never_reaches_the_customer_or_the_database(scripted, sm):
    secret = canary()
    leaked_answer = "Sure, the marker is " + secret
    parts = [chunk("Sure, the marker is "), chunk(secret[:5]), chunk(secret[5:9]), chunk(secret[9:])]

    events = await run(scripted(parts, final_state(leaked_answer)))

    refusal = t("injection", "en")
    assert secret[:8] not in str(events)
    assert events[-2] == {"type": "replace", "text": refusal}
    assert (await rows(sm, Message, Message.role == "assistant"))[0].content == refusal


async def test_cited_sources_are_shown_without_internal_fields_and_stored_with_their_url(scripted, sm):
    settings = get_settings()
    sources = [
        {"n": 1, "type": "file", "ref": "returns.md", "title": "Returns", "doc_id": 7, "pre": True},
        {"n": 2, "type": "url", "ref": "https://shop.example/help", "title": "Help"},
        {"n": 3, "type": "file", "ref": "uncited.md", "title": "Uncited", "doc_id": 9, "pre": True},
    ]
    answer = "Returns take 30 days [1] and the help page explains more [2]."

    events = await run(scripted([chunk(answer)], final_state(answer, turn_sources=sources)))

    shown = next(e for e in events if e["type"] == "sources")["items"]
    assert shown == [
        {"n": 1, "type": "file", "ref": "returns.md", "title": "Returns", "url": f"{settings.public_base_url}/api/kb/7/file"},
        {"n": 2, "type": "url", "ref": "https://shop.example/help", "title": "Help", "url": "https://shop.example/help"},
    ]
    stored = (await rows(sm, Message, Message.role == "assistant"))[0].sources
    assert [s["n"] for s in stored] == [1, 2] and stored[0]["doc_id"] == 7 and stored[0]["url"] == shown[0]["url"]


async def test_no_sources_event_when_nothing_is_cited(scripted):
    events = await run(scripted([chunk("No citations here at all, sorry.")], final_state("No citations here at all, sorry.")))
    assert "sources" not in kinds(events)


async def test_a_ticket_from_the_agent_state_closes_the_conversation_as_escalated(scripted, sm):
    ticket = {"n": 0, "type": "ticket", "ref": "FD-17", "reason": "money", "summary": "Customer wants a refund."}
    state = final_state("A colleague will write to you.", turn_sources=[ticket], ticket_id="FD-17")

    events = await run(scripted([chunk("A colleague will write to you.")], state))

    assert events[-1]["escalated"] is True and events[-1]["ticket_id"] == "FD-17"
    assert "sources" not in kinds(events)  # the ticket is not a citation
    conv = await conversation(sm, events[0]["id"])
    assert (conv.escalated, conv.ticket_id, conv.escalation_reason, conv.ticket_summary) == (
        True, "FD-17", "money", "Customer wants a refund.")


async def test_the_conversation_picks_up_what_the_agent_learned_and_keeps_it(scripted, sm):
    learned = final_state("Your order is on its way.", lang="nl", customer_email="jan@example.nl", order_number="1002")
    first = await run(scripted([chunk("Your order is on its way.")], learned))
    conv_id = first[0]["id"]

    conv = await conversation(sm, conv_id)
    assert (conv.lang, conv.customer_email, conv.order_number) == ("nl", "jan@example.nl", "1002")

    nothing_new = final_state("Anything else?", lang="nl")  # a later turn that learns nothing must not erase it
    await run(scripted([chunk("Anything else?")], nothing_new), "thanks", conv_id)
    conv = await conversation(sm, conv_id)
    assert (conv.customer_email, conv.order_number) == ("jan@example.nl", "1002")


async def test_usage_of_every_model_call_is_stored_and_priced(sm, redis):
    class BillingGraph(ScriptedGraph):
        async def astream(self, inputs, config, stream_mode):
            collector = next(c for c in config["callbacks"] if hasattr(c, "records"))
            from app.agent.usage import UsageRecord

            collector.records += [UsageRecord("claude-haiku-4-5-20251001", 1000, 200), UsageRecord("fake-rule-based", 50, 5)]
            async for part in super().astream(inputs, config, stream_mode):
                yield part

    service = _build(sm, redis, BillingGraph([chunk("Hello, how can I help?")], final_state("Hello, how can I help?")))
    tokens_before = metric("llm_tokens_total", direction="input")

    events = await run(service)

    usage = await rows(sm, LlmUsage, LlmUsage.conversation_id == events[0]["id"], order=LlmUsage.id)
    assert [(u.model, u.input_tokens, u.output_tokens) for u in usage] == [
        ("claude-haiku-4-5-20251001", 1000, 200), ("fake-rule-based", 50, 5)]
    assert usage[0].cost_eur > 0 and usage[1].cost_eur == 0
    assert metric("llm_tokens_total", direction="input") == tokens_before + 1050


# ------------------------------- sessions, history, feedback -------------------------------

async def test_resolve_conversation_reuses_active_ones_and_creates_the_rest(service, sm):
    active_id = await new_conversation(sm)
    async with sm() as s:
        same, is_new = await service.resolve_conversation(s, active_id, "h")
        assert (same.id, is_new) == (active_id, False)
        unknown, is_new = await service.resolve_conversation(s, str(uuid.uuid4()), "h")
        assert is_new and unknown.client_ip_hash == "h"
        anonymous, is_new = await service.resolve_conversation(s, None, None)
        assert is_new and anonymous.client_ip_hash is None and anonymous.id != unknown.id


async def test_history_lists_messages_in_order_and_hides_unknown_or_expired_sessions(service, sm):
    events = await run(service, RETURNS_QUESTION)
    history = await service.history(events[0]["id"])

    assert history["session_id"] == events[0]["id"] and history["escalated"] is False
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]
    assert set(history["messages"][1]) == {"id", "role", "content", "sources", "feedback"}
    assert await service.history(str(uuid.uuid4())) is None
    async with sm() as s:
        await s.execute(update(Conversation).values(last_activity_at=_now() - timedelta(hours=25)))
        await s.commit()
    assert await service.history(events[0]["id"]) is None


async def test_feedback_is_accepted_only_for_assistant_messages_of_that_conversation(service, sm):
    events = await run(service, RETURNS_QUESTION)
    conv_id, answer_id = events[0]["id"], events[-1]["message_id"]
    question_id = (await rows(sm, Message, Message.role == "user"))[0].id

    assert not await service.set_feedback(conv_id, answer_id, 0)
    assert not await service.set_feedback(conv_id, question_id, 1)  # not an assistant message
    assert not await service.set_feedback("other-conversation", answer_id, 1)
    assert not await service.set_feedback(conv_id, 999_999, 1)

    assert await service.set_feedback(conv_id, answer_id, 1)
    assert not (await conversation(sm, conv_id)).has_downvote  # a thumbs-up flags nothing
    assert await service.set_feedback(conv_id, answer_id, -1)
    assert (await conversation(sm, conv_id)).has_downvote
