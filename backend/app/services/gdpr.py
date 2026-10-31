"""GDPR: retention (90 days), deletion by customer email, expiry of idle sessions."""

import logging
from datetime import timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, UnansweredQuestion, _now

log = logging.getLogger(__name__)

_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


async def delete_conversations(session: AsyncSession, ids: list[str]) -> None:
    if not ids:
        return
    for table in _CHECKPOINT_TABLES:  # tables are created by the LangGraph Postgres saver
        exists = (await session.execute(text("SELECT to_regclass(:t)"), {"t": table})).scalar()
        if exists:
            await session.execute(text(f"DELETE FROM {table} WHERE thread_id = ANY(:ids)"), {"ids": ids})  # noqa: S608
    await session.execute(delete(UnansweredQuestion).where(UnansweredQuestion.conversation_id.in_(ids)))
    await session.execute(delete(Conversation).where(Conversation.id.in_(ids)))


async def delete_customer_data(session: AsyncSession, email: str) -> int:
    ids = list((await session.execute(
        select(Conversation.id).where(Conversation.customer_email == email.strip().lower())
    )).scalars())
    await delete_conversations(session, ids)
    await session.commit()
    log.info("gdpr erasure executed conversations=%d", len(ids), extra={"event": "gdpr_erasure"})
    return len(ids)


async def purge_expired(session: AsyncSession, retention_days: int) -> int:
    cutoff = _now() - timedelta(days=retention_days)
    ids = list((await session.execute(select(Conversation.id).where(Conversation.last_activity_at < cutoff))).scalars())
    # drop every batch of 500 to keep statements small
    for i in range(0, len(ids), 500):
        await delete_conversations(session, ids[i : i + 500])
    await session.commit()
    log.info("retention purge removed=%d", len(ids), extra={"event": "retention_purge"})
    return len(ids)
