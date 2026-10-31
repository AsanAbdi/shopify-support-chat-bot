"""Human handover: operator summary (3-5 lines) + Freshdesk ticket with the full history."""

import logging
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.agent.prompts import TICKET_SUMMARY_SYSTEM
from app.agent.text import message_text
from app.config import Settings
from app.integrations.errors import IntegrationError
from app.logging_conf import mask_pii

log = logging.getLogger(__name__)

LANG_NAMES = {"de": "German", "en": "English", "nl": "Dutch"}


@dataclass
class EscalationResult:
    ticket_id: str | None
    summary: str


def plain_history(messages: list[BaseMessage]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in messages:
        text = message_text(m)
        if isinstance(m, HumanMessage) and text.strip():
            out.append(("user", text))
        elif isinstance(m, AIMessage) and text.strip() and not m.tool_calls:
            out.append(("assistant", text))
    return out


async def summarize_for_operator(llm, messages: list[BaseMessage], *, conv_summary: str, lang: str, reason: str) -> str:
    history = plain_history(messages)
    transcript = "\n".join(f"{'Customer' if r == 'user' else 'Bot'}: {t}" for r, t in history)
    if conv_summary:
        transcript = f"[earlier conversation summary] {conv_summary}\n{transcript}"
    system = TICKET_SUMMARY_SYSTEM.format(language=LANG_NAMES.get(lang, "English"), reason=reason)
    try:
        res = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=transcript[-8000:])])
        return message_text(res).strip()
    except Exception:
        log.exception("ticket summary failed; falling back to last customer messages")
        return "\n".join(f"- {t[:200]}" for r, t in history if r == "user")[-800:]


async def create_ticket(
    *, freshdesk, llm, settings: Settings, messages: list[BaseMessage], conv_summary: str, lang: str, email: str,
    order_number: str | None, reason: str,
) -> EscalationResult:
    summary = await summarize_for_operator(llm, messages, conv_summary=conv_summary, lang=lang, reason=reason)
    history = plain_history(messages)
    first_q = next((t for r, t in history if r == "user"), "Customer request")
    subject = f"[Chat] {'#' + order_number + ' - ' if order_number else ''}{first_q[:90]}"
    try:
        ticket_id = await freshdesk.create_ticket(
            email=email, subject=subject, summary=summary, history=history, lang=lang,
            order_number=order_number, reason=reason, urgent=reason in {"complaint", "money"},
        )
    except IntegrationError:
        log.exception("freshdesk ticket creation failed (customer=%s)", mask_pii(email))
        return EscalationResult(None, summary)
    return EscalationResult(ticket_id, summary)
