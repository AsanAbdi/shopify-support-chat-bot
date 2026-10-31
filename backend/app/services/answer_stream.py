"""Turns what LangGraph streams during a turn into the events the widget understands."""

import time
from collections.abc import Iterator
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from app.agent.text import content_text
from app.observability import TTFT
from app.services.leak_guard import LeakGuard

Event = dict[str, Any]


class AnswerStreamer:
    """Feed it the graph's stream parts; it yields widget events and remembers what the customer currently sees.

    The graph is streamed with `stream_mode=["messages", "updates"]`, which gives two kinds of parts:

      ("messages", (chunk, meta))   a piece of text from some model call. Only the `agent` node writes the answer.
      ("updates", {node: update})   a node has finished. Two of them change what the customer should see:
                                    `agent` ending in a tool call: what it wrote before was a draft, discard it;
                                    `escalate`: its fixed handover message is not streamed by a model, send it whole.
    """

    def __init__(self) -> None:
        self.shown = ""  # the answer as the widget displays it right now
        self._guard = LeakGuard()
        self._started = time.monotonic()
        self._first_token_sent = False

    def on_part(self, mode: str, payload: Any) -> Iterator[Event]:
        if mode == "messages":
            chunk, meta = payload
            yield from self._on_model_chunk(chunk, meta)
        else:
            yield from self._on_node_updates(payload or {})

    def finish(self) -> Iterator[Event]:
        """The graph is done: release the text the leak guard was still holding back."""
        tail = self._guard.flush()
        if tail:
            self.shown += tail
            yield {"type": "token", "text": tail}

    def _on_model_chunk(self, chunk: Any, meta: dict) -> Iterator[Event]:
        if meta.get("langgraph_node") != "agent" or not isinstance(chunk, AIMessageChunk):
            return
        piece = self._guard.feed(content_text(chunk.content))
        if not piece:
            return
        if not self._first_token_sent:
            self._first_token_sent = True
            TTFT.observe(time.monotonic() - self._started)
        self.shown += piece
        yield {"type": "token", "text": piece}

    def _on_node_updates(self, updates: dict) -> Iterator[Event]:
        for node, update in updates.items():
            messages = (update or {}).get("messages") or []
            last = messages[-1] if messages else None
            if node == "agent" and isinstance(last, AIMessage) and last.tool_calls:
                yield from self._discard_draft()
            elif node == "escalate" and isinstance(last, AIMessage):
                self.shown = content_text(last.content)
                yield {"type": "token", "text": self.shown}

    def _discard_draft(self) -> Iterator[Event]:
        if self.shown:  # the model wrote a preamble before calling a tool: the widget must forget it
            yield {"type": "reset"}
        self.shown = ""
        self._guard.reset()
