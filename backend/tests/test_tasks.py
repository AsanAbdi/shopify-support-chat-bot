"""Celery tasks executed for real (eagerly, in a thread because they run their own event loop)."""

import asyncio

import httpx
from celery.schedules import crontab
from sqlalchemy import func, select, text

from app.models import Chunk, Conversation, Document, Product
from app.workers.celery_app import celery
from app.workers.tasks import (
    index_document_task,
    refresh_url_documents,
    reindex_all_task,
    retention_task,
    sync_catalog_task,
)
from tests.conftest import MOCK_BASE


async def _run(task, *args):
    res = await asyncio.to_thread(lambda: task.apply(args=args))
    assert res.successful(), res.traceback
    return res.result


def test_beat_schedule_is_wired_to_registered_tasks_at_night_hours():
    sched = celery.conf.beat_schedule
    assert {v["task"] for v in sched.values()} == {
        "app.workers.tasks.sync_catalog_task", "app.workers.tasks.refresh_url_documents", "app.workers.tasks.retention_task",
    }
    assert all(v["task"] in celery.tasks for v in sched.values())
    hours = {v["task"].rsplit(".", 1)[-1]: v["schedule"] for v in sched.values()}
    assert hours["sync_catalog_task"] == crontab(hour=3, minute=0) and hours["retention_task"] == crontab(hour=4, minute=0)
    assert str(celery.conf.timezone) == "Europe/Berlin"


async def test_catalog_sync_task_fills_products_and_is_incremental(session):
    first = await _run(sync_catalog_task)
    assert first["upserted"] > 60 and first["embedded"] == first["upserted"]
    assert (await session.execute(select(func.count()).select_from(Product))).scalar() == first["upserted"]
    second = await _run(sync_catalog_task)
    assert second["embedded"] == 0 and second["removed"] == 0


async def test_retention_task_deletes_only_conversations_older_than_90_days(session):
    session.add_all([Conversation(id="old-1"), Conversation(id="fresh-1")])
    await session.commit()
    await session.execute(text("UPDATE conversations SET last_activity_at = now() - interval '91 days' WHERE id = 'old-1'"))
    await session.commit()
    assert await _run(retention_task) == 1
    remaining = (await session.execute(select(Conversation.id))).scalars().all()
    assert remaining == ["fresh-1"]


async def test_url_document_is_indexed_and_only_changes_are_reembedded(session):
    url = f"{MOCK_BASE}/pages/gift-wrap.html"
    doc = Document(title=url, source_type="url", source_ref=url)
    session.add(doc)
    await session.commit()
    doc_id = doc.id

    first = await _run(index_document_task, doc_id)
    assert first["status"] == "indexed" and first["chunks"] == 2
    chunks = (await session.execute(select(Chunk.content).where(Chunk.document_id == doc_id))).scalars().all()
    joined = "\n".join(chunks)
    assert "free gift wrapping" in joined and "Home | Shop" not in joined and "Imprint" not in joined  # chrome stripped
    session.expire_all()
    assert (await session.get(Document, doc_id)).title == "Gift wrapping"

    assert (await _run(refresh_url_documents)) >= 1  # the daily refresh: nothing changed -> nothing re-embedded
    session.expire_all()
    assert (await _run(index_document_task, doc_id))["status"] == "unchanged"

    async with httpx.AsyncClient() as c:
        page = (await c.get(f"{MOCK_BASE}/pages/gift-wrap.html")).text.replace("EUR 2", "EUR 3")
        await c.put(f"{MOCK_BASE}/pages/gift-wrap.html", content=page)
    changed = await _run(refresh_url_documents)
    assert changed >= 1
    session.expire_all()
    new_text = "\n".join((await session.execute(select(Chunk.content).where(Chunk.document_id == doc_id))).scalars().all())
    assert "EUR 3" in new_text and "EUR 2" not in new_text
    async with httpx.AsyncClient() as c:  # restore the shared mock page for other tests
        await c.put(f"{MOCK_BASE}/pages/gift-wrap.html", content=page.replace("EUR 3", "EUR 2"))


async def test_pdf_url_is_indexed(session):
    url = f"{MOCK_BASE}/files/terms.pdf"
    doc = Document(title="terms", source_type="url", source_ref=url)
    session.add(doc)
    await session.commit()
    res = await _run(index_document_task, doc.id)
    assert res["status"] == "indexed"
    text_ = (await session.execute(select(Chunk.content).where(Chunk.document_id == doc.id))).scalars().first()
    assert "30 days of delivery" in text_


async def test_unreachable_url_marks_the_document_failed_with_a_reason(session):
    doc = Document(title="x", source_type="url", source_ref=f"{MOCK_BASE}/pages/does-not-exist.html")
    session.add(doc)
    await session.commit()
    doc_id = doc.id
    assert (await _run(index_document_task, doc_id))["status"] == "failed"
    session.expire_all()
    d = await session.get(Document, doc_id)
    assert d.status == "failed" and "404" in d.error


async def test_reindex_all_restores_vectors_after_a_model_change(session, seeded_kb):
    from sqlalchemy import text

    n_chunks = (await session.execute(select(func.count()).select_from(Chunk))).scalar()
    await session.execute(text("DELETE FROM chunks"))  # what migration 0003 does when the embedding dimension changes
    await session.execute(text("UPDATE documents SET content_hash = NULL, chunks_count = 0, status = 'pending'"))
    await session.commit()

    result = await _run(reindex_all_task)
    assert result["documents"] == 11 and result["failed"] == 0 and result["catalog"]["upserted"] > 60
    session.expire_all()
    assert (await session.execute(select(func.count()).select_from(Chunk))).scalar() == n_chunks
    assert (await session.execute(select(func.count()).select_from(Document).where(Document.status == "indexed"))).scalar() == 11
