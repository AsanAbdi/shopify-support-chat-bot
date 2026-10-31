import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.integrations.catalog import sync_catalog
from app.integrations.shopify import ShopifyClient
from app.models import Document
from app.rag.embeddings import get_embedder
from app.rag.indexer import index_document
from app.services.gdpr import purge_expired
from app.workers.celery_app import celery

log = logging.getLogger(__name__)


@asynccontextmanager
async def task_session():
    """Each task runs in its own event loop, so it gets its own engine (NullPool) instead of the shared one."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


def _run(fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    async def main() -> Any:
        async with task_session() as session:
            return await fn(session)

    return asyncio.run(main())


@celery.task(name="app.workers.tasks.index_document_task", autoretry_for=(OSError,), retry_backoff=True, max_retries=3)
def index_document_task(document_id: int, force: bool = False) -> dict:
    return _run(lambda s: index_document(s, document_id, force=force))


@celery.task(name="app.workers.tasks.refresh_url_documents")
def refresh_url_documents() -> int:
    """Re-fetch URL sources daily; the content hash decides whether anything is actually re-embedded."""
    async def go(session: AsyncSession) -> int:
        ids = list((await session.execute(select(Document.id).where(Document.source_type == "url"))).scalars())
        for doc_id in ids:
            await index_document(session, doc_id)
        return len(ids)

    return _run(go)


@celery.task(name="app.workers.tasks.sync_catalog_task")
def sync_catalog_task() -> dict:
    async def go(session: AsyncSession) -> dict:
        shopify = ShopifyClient()
        try:
            return await sync_catalog(session, shopify, get_embedder())
        finally:
            await shopify.aclose()

    return _run(go)


@celery.task(name="app.workers.tasks.retention_task")
def retention_task() -> int:
    return _run(lambda s: purge_expired(s, get_settings().conversation_retention_days))


@celery.task(name="app.workers.tasks.reindex_all_task")
def reindex_all_task() -> dict:
    """Re-embed every document and the catalog. Needed after changing the embedding model or restoring from a backup
    made with another model (migration 0003 clears the vectors and relies on this)."""
    async def go(session: AsyncSession) -> dict:
        ids = list((await session.execute(select(Document.id))).scalars())
        result = {"documents": len(ids), "failed": 0}
        for doc_id in ids:
            if (await index_document(session, doc_id, force=True))["status"] == "failed":
                result["failed"] += 1
        shopify = ShopifyClient()
        try:
            result["catalog"] = await sync_catalog(session, shopify, get_embedder(), force=True)
        except Exception as exc:  # the catalog may be unreachable; documents are done anyway
            log.exception("catalog sync during reindex-all failed")
            result["catalog"] = {"error": type(exc).__name__}
        finally:
            await shopify.aclose()
        return result

    return _run(go)
