"""AnswerStreamer: LangGraph stream parts in, widget events out. No database, no Redis, no model."""

from langchain_core.messages import AIMessage, AIMessageChunk
from prometheus_client import REGISTRY

from app.agent.prompts import canary
from app.services.answer_stream import AnswerStreamer

HOLD = len(canary()) - 1  # the leak guard keeps this many trailing characters back until it knows they are safe
TOOL_CALL = AIMessage(content="", tool_calls=[{"name": "search_knowledge_base", "args": {"query": "q"}, "id": "1"}])


def model_text(text: str, node: str = "agent") -> tuple:
    return "messages", (AIMessageChunk(content=text), {"langgraph_node": node})


def node_done(node: str, message: AIMessage) -> tuple:
    return "updates", {node: {"messages": [message]}}


def feed(streamer: AnswerStreamer, *parts: tuple) -> list[dict]:
    return [event for part in parts for event in streamer.on_part(*part)]


def first_token_count() -> float:
    return REGISTRY.get_sample_value("chat_first_token_seconds_count") or 0.0


def test_text_shorter_than_the_hold_back_window_appears_only_when_the_graph_finishes():
    streamer = AnswerStreamer()
    assert feed(streamer, model_text("Hi there")) == []
    assert list(streamer.finish()) == [{"type": "token", "text": "Hi there"}]
    assert streamer.shown == "Hi there"


def test_long_text_streams_ahead_and_the_held_back_tail_follows_on_finish():
    streamer = AnswerStreamer()
    events = feed(streamer, model_text("a" * 40))

    assert events == [{"type": "token", "text": "a" * (40 - HOLD)}]
    assert list(streamer.finish()) == [{"type": "token", "text": "a" * HOLD}]
    assert streamer.shown == "a" * 40


def test_only_the_agent_nodes_model_chunks_are_part_of_the_answer():
    streamer = AnswerStreamer()
    parts = [
        model_text("t" * 60, node="tools"),
        model_text("s" * 60, node="prepare"),
        ("messages", (AIMessage(content="m" * 60), {"langgraph_node": "agent"})),  # a whole message, not a chunk
    ]
    assert feed(streamer, *parts) == [] and list(streamer.finish()) == [] and streamer.shown == ""


def test_provider_content_blocks_contribute_only_their_text():
    blocks = AIMessageChunk(content=[{"type": "text", "text": "x" * 40}, {"type": "tool_use", "id": "1", "name": "n", "input": {}}])
    streamer = AnswerStreamer()

    events = feed(streamer, ("messages", (blocks, {"langgraph_node": "agent"})))

    assert events == [{"type": "token", "text": "x" * (40 - HOLD)}]


def test_a_tool_call_after_some_text_resets_the_widget_and_drops_the_held_back_tail():
    streamer = AnswerStreamer()
    feed(streamer, model_text("draft " * 10))

    events = feed(streamer, node_done("agent", TOOL_CALL))

    assert events == [{"type": "reset"}]
    assert streamer.shown == "" and list(streamer.finish()) == []  # nothing of the draft may come back


def test_a_tool_call_without_any_shown_text_needs_no_reset():
    streamer = AnswerStreamer()
    feed(streamer, model_text("short"))  # held back, so the customer has seen nothing yet

    assert feed(streamer, node_done("agent", TOOL_CALL)) == []
    assert list(streamer.finish()) == []  # the held-back draft was dropped as well


def test_an_agent_turn_without_tool_calls_changes_nothing():
    streamer = AnswerStreamer()
    feed(streamer, model_text("answer " * 10))
    shown = streamer.shown

    assert feed(streamer, node_done("agent", AIMessage(content="answer"))) == []
    assert streamer.shown == shown


def test_the_handover_message_is_sent_whole_and_replaces_what_was_shown():
    streamer = AnswerStreamer()
    feed(streamer, model_text("something " * 10))

    events = feed(streamer, node_done("escalate", AIMessage(content="Please share your e-mail.")))

    assert events == [{"type": "token", "text": "Please share your e-mail."}]
    assert streamer.shown == "Please share your e-mail."


def test_empty_or_missing_updates_are_ignored():
    streamer = AnswerStreamer()
    assert feed(streamer, ("updates", None), ("updates", {}), ("updates", {"agent": None}), ("updates", {"agent": {}})) == []


def test_the_system_prompt_marker_never_leaves_even_when_split_over_chunks():
    secret = canary()
    streamer = AnswerStreamer()
    parts = [model_text("Sure, the marker is "), model_text(secret[:5]), model_text(secret[5:9]), model_text(secret[9:])]

    emitted = "".join(e["text"] for e in feed(streamer, *parts)) + "".join(e["text"] for e in streamer.finish())

    assert secret[:6] not in emitted and secret[:6] not in streamer.shown


def test_time_to_first_token_is_measured_once_and_only_for_streamed_model_text():
    before = first_token_count()
    streamer = AnswerStreamer()
    feed(streamer, model_text("a" * 40), model_text("b" * 40))
    list(streamer.finish())
    assert first_token_count() == before + 1

    before = first_token_count()
    handover = AnswerStreamer()
    feed(handover, node_done("escalate", AIMessage(content="Please share your e-mail.")))
    short = AnswerStreamer()
    feed(short, model_text("Yes."))
    list(short.finish())  # a short answer only comes out of the hold-back window at the end
    assert first_token_count() == before
