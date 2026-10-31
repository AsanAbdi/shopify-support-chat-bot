"""One chat turn: lock the conversation, store the question, run the agent, stream its answer, store the result.

Events sent to the widget (`widget.js: handle()` is the other half of this contract):

    session  {id, new}                          a conversation was created for this message
    token    {text}                             a piece of the answer
    reset    {}                                 drop everything shown so far (a draft written before a tool call)
    replace  {text}                             the final answer differs from what was streamed: show this one
    sources  {items}                            citations of the answer
    done     {message_id, escalated, ticket_id} the turn is finished
    error    {code, text}                       refused or failed; code is invalid_message, busy or internal
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Self

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import contains_canary
from app.agent.i18n import t
from app.agent.sources import as_shown, as_stored, select_cited
from app.agent.text import content_text
from app.agent.usage import UsageCollector, cost_eur
from app.db import session_factory
from app.deps import Container
from app.logging_conf import mask_pii
from app.models import Conversation, LlmUsage, Message, UnansweredQuestion, _now
from app.observability import (
    BLOCKED,
    CHAT_TURNS,
    ESCALATIONS,
    LLM_COST_EUR,
    LLM_TOKENS,
    NO_ANSWER,
    TURN_SECONDS,
    langfuse_callbacks,
)
from app.services.answer_stream import AnswerStreamer, Event
from app.services.budget import check_and_alert
from app.services.turn_lock import TurnLock

log = logging.getLogger(__name__)

GRAPH_RECURSION_LIMIT = 30  # node executions per turn: stops an agent that keeps calling tools
BUDGET_EXHAUSTED = 1.0  # share of the monthly LLM budget at which the assistant pauses
MAX_STORED_QUESTION_CHARS = 1000


def _error(code: str, lang: str) -> Event:
    return {"type": "error", "code": code, "text": t("error", lang)}


@dataclass(frozen=True)
class AgentTurn:
    """What the agent produced in one turn, ready to be stored and shown."""

    question: str
    answer: str
    state: dict  # the graph's final state: language, e-mail, order number, ticket, ...
    cited: list[dict]  # the sources the answer actually cites
    ticket: dict | None  # set when this turn handed the conversation over to a human
    no_answer: bool  # the knowledge base had nothing for this question

    @classmethod
    def from_state(cls, state: dict, *, question: str, lang: str) -> Self:
        last_reply = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)
        answer = content_text(last_reply.content).strip() if last_reply else ""
        if contains_canary(answer):  # belt and braces: the graph's `finalize` node already replaces it
            answer = t("injection", state.get("lang", lang))
        sources = state.get("turn_sources", [])
        return cls(
            question=question,
            answer=answer,
            state=state,
            cited=select_cited(answer, sources),
            ticket=next((s for s in sources if s.get("type") == "ticket"), None),
            no_answer=bool(state.get("turn_no_answer")),
        )


class ChatService:
    def __init__(self, c: Container) -> None:
        self.c = c
        self.s = c.settings

    @staticmethod
    def _session() -> AsyncSession:
        return session_factory()()

    # ------------------------------ conversations ------------------------------

    def _is_active(self, conv: Conversation) -> bool:
        return _now() - conv.last_activity_at <= timedelta(hours=self.s.session_ttl_hours)

    async def resolve_conversation(
        self, session: AsyncSession, conv_id: str | None, ip_hash: str | None
    ) -> tuple[Conversation, bool]:
        """The customer's conversation if it is still active, otherwise a new one. Returns (conversation, is_new)."""
        conv = await session.get(Conversation, conv_id) if conv_id else None
        if conv is not None and self._is_active(conv):
            return conv, False
        conv = Conversation(id=str(uuid.uuid4()), client_ip_hash=ip_hash)
        session.add(conv)
        await session.commit()
        return conv, True

    async def history(self, conv_id: str) -> dict | None:
        async with self._session() as session:
            conv = await session.get(Conversation, conv_id)
            if conv is None or not self._is_active(conv):
                return None
            query = select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)
            messages = (await session.execute(query)).scalars().all()
            return {
                "session_id": conv.id, "lang": conv.lang, "escalated": conv.escalated,
                "messages": [
                    {"id": m.id, "role": m.role, "content": m.content, "sources": m.sources, "feedback": m.feedback}
                    for m in messages
                ],
            }

    async def set_feedback(self, conv_id: str, message_id: int, value: int) -> bool:
        if value not in (1, -1):
            return False
        async with self._session() as session:
            message = await session.get(Message, message_id)
            if message is None or message.conversation_id != conv_id or message.role != "assistant":
                return False
            message.feedback = value
            if value == -1:
                conv = await session.get(Conversation, conv_id)
                if conv:
                    conv.has_downvote = True
            await session.commit()
            return True

    # ------------------------------ the turn ------------------------------

    async def stream_turn(self, conv_id: str | None, text: str, ip_hash: str | None = None) -> AsyncIterator[Event]:
        text = text.strip()
        if not text or len(text) > self.s.max_user_message_chars:
            BLOCKED.labels("bad_length").inc()
            yield _error("invalid_message", "en")
            return

        conv_id, lang, is_new = await self._open_conversation(conv_id, ip_hash)
        if is_new:
            yield {"type": "session", "id": conv_id, "new": True}

        lock = TurnLock(self.c.redis, conv_id)
        if not await lock.acquire():
            BLOCKED.labels("busy").inc()
            yield _error("busy", lang)
            return

        started, outcome = time.monotonic(), "ok"
        try:
            if await self._store_question(conv_id, text) >= BUDGET_EXHAUSTED:
                outcome = "budget"
                notice = t("budget", lang, support_email=self.s.support_email)
                yield {"type": "token", "text": notice}
                await self._store_assistant_reply(conv_id, notice)
                yield {"type": "done", "escalated": False}
                return

            async for event in self._run_agent(conv_id, text, lang):
                yield event
        except Exception:
            outcome = "error"
            log.exception("chat turn failed", extra={"conversation_id": conv_id})
            yield _error("internal", lang)
        finally:
            await lock.release()
            CHAT_TURNS.labels(lang, outcome).inc()
            TURN_SECONDS.observe(time.monotonic() - started)

    async def _open_conversation(self, conv_id: str | None, ip_hash: str | None) -> tuple[str, str, bool]:
        """Returns (conversation id, language, is_new). The id differs from `conv_id` when the old one expired."""
        async with self._session() as session:
            conv, is_new = await self.resolve_conversation(session, conv_id, ip_hash)
            return conv.id, conv.lang, is_new

    async def _run_agent(self, conv_id: str, text: str, lang: str) -> AsyncIterator[Event]:
        usage = UsageCollector()
        config = self._graph_config(conv_id, usage)
        stream = AnswerStreamer()

        async for mode, payload in self.c.graph.astream(
            {"messages": [HumanMessage(content=text)]}, config, stream_mode=["messages", "updates"]
        ):
            for event in stream.on_part(mode, payload):
                yield event
        for event in stream.finish():
            yield event

        state = (await self.c.graph.aget_state(config)).values
        turn = AgentTurn.from_state(state, question=text, lang=lang)
        if stream.shown.strip() != turn.answer:  # the final answer was changed after streaming (e.g. leak guard)
            yield {"type": "replace", "text": turn.answer}

        message_id = await self._store_agent_turn(conv_id, turn, usage)
        if turn.cited:
            yield {"type": "sources", "items": [as_shown(s, self.s.public_base_url) for s in turn.cited]}
        yield {
            "type": "done", "message_id": message_id,
            "escalated": bool(state.get("ticket_id")), "ticket_id": state.get("ticket_id"),
        }

    def _graph_config(self, conv_id: str, usage: UsageCollector) -> dict:
        return {
            "configurable": {"thread_id": conv_id, "deps": self.c.graph_deps},  # thread_id = the agent's memory
            "callbacks": [usage, *langfuse_callbacks(self.c.langfuse_enabled)],
            "metadata": {"langfuse_session_id": conv_id},
            "run_name": "support_turn",
            "recursion_limit": GRAPH_RECURSION_LIMIT,
        }

    # ------------------------------ storing ------------------------------

    async def _store_question(self, conv_id: str, text: str) -> float:
        """Saves the customer's message. Returns the share of the monthly LLM budget that is already spent."""
        async with self._session() as session:
            session.add(Message(conversation_id=conv_id, role="user", content=text))
            await session.commit()
            return await check_and_alert(session, self.c.redis, self.s)

    async def _store_assistant_reply(self, conv_id: str, content: str) -> None:
        async with self._session() as session:
            session.add(Message(conversation_id=conv_id, role="assistant", content=content))
            conv = await session.get(Conversation, conv_id)
            if conv:
                conv.last_activity_at = _now()
            await session.commit()

    async def _store_agent_turn(self, conv_id: str, turn: AgentTurn, usage: UsageCollector) -> int:
        """Saves the answer, updates the conversation, records unanswered questions and LLM usage. Returns the message id."""
        async with self._session() as session:
            message = Message(
                conversation_id=conv_id, role="assistant", content=turn.answer, no_answer=turn.no_answer,
                sources=[as_stored(s, self.s.public_base_url) for s in turn.cited],
            )
            session.add(message)
            conv = await session.get(Conversation, conv_id)
            if conv:
                self._update_conversation(conv, turn)
            if turn.no_answer:
                NO_ANSWER.inc()
                session.add(UnansweredQuestion(
                    conversation_id=conv_id, question=mask_pii(turn.question)[:MAX_STORED_QUESTION_CHARS]
                ))
            self._record_usage(session, conv_id, usage)
            await session.commit()
            await session.refresh(message)
            return message.id

    @staticmethod
    def _update_conversation(conv: Conversation, turn: AgentTurn) -> None:
        state = turn.state
        conv.last_activity_at = _now()
        conv.lang = state.get("lang", conv.lang)
        conv.customer_email = state.get("customer_email") or conv.customer_email
        conv.order_number = state.get("order_number") or conv.order_number
        conv.had_no_answer = conv.had_no_answer or turn.no_answer
        if turn.ticket:
            conv.escalated, conv.ticket_id = True, turn.ticket["ref"]
            conv.escalation_reason, conv.ticket_summary = turn.ticket.get("reason"), turn.ticket.get("summary")
            ESCALATIONS.labels(turn.ticket.get("reason") or "other").inc()

    def _record_usage(self, session: AsyncSession, conv_id: str, usage: UsageCollector) -> None:
        """One row per model call of this turn (agent, conversation summary, ticket summary)."""
        for record in usage.records:
            cost = cost_eur(record, self.s)
            session.add(LlmUsage(
                conversation_id=conv_id, model=record.model, input_tokens=record.input_tokens,
                output_tokens=record.output_tokens, cost_eur=cost,
            ))
            LLM_COST_EUR.inc(float(cost))
            LLM_TOKENS.labels("input").inc(record.input_tokens)
            LLM_TOKENS.labels("output").inc(record.output_tokens)
