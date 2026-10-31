"""Incremental indexing: unchanged documents are skipped, changed ones re-embed only the chunks that changed."""

import logging
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Chunk, Document, _now
from app.rag.chunking import chunk_document, document_hash
from app.rag.embeddings import get_embedder
from app.rag.loaders import LoadedDocument, load_file, load_url

log = logging.getLogger(__name__)


async def _load(doc: Document) -> LoadedDocument:
    settings = get_settings()
    if doc.source_type == "url":
        _, loaded = load_url(doc.source_ref, allow_private=settings.url_fetch_allow_private)
    else:
        data = Path(doc.stored_path or "").read_bytes()
        loaded = load_file(data, doc.source_ref)
    return loaded


async def index_document(session: AsyncSession, document_id: int, *, force: bool = False) -> dict:
    settings = get_settings()
    doc = await session.get(Document, document_id)
    if doc is None:
        return {"status": "missing"}
    doc.status, doc.error = "indexing", None
    await session.commit()

    try:
        loaded = await _load(doc)
        new_hash = document_hash(loaded)
        if not force and doc.content_hash == new_hash and doc.chunks_count > 0:
            doc.status = "indexed"
            await session.commit()
            return {"status": "unchanged", "chunks": doc.chunks_count}

        drafts = chunk_document(loaded, chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap)
        existing = {
            c.content_hash: c
            for c in (await session.execute(select(Chunk).where(Chunk.document_id == doc.id))).scalars()
        }
        wanted = {d.content_hash for d in drafts}
        stale_ids = [c.id for h, c in existing.items() if h not in wanted]
        if stale_ids:
            await session.execute(delete(Chunk).where(Chunk.id.in_(stale_ids)))

        fresh = [d for d in drafts if d.content_hash not in existing]
        # Normally only new chunks are embedded. `force` means "recompute everything": the vectors of unchanged chunks
        # are replaced too (needed after a model change that kept the dimension, otherwise they would silently go stale).
        to_embed = drafts if force else fresh
        vectors = await get_embedder().embed_documents([d.content for d in to_embed]) if to_embed else []
        fresh_by_hash = dict(zip((d.content_hash for d in to_embed), vectors, strict=True))
        for idx, d in enumerate(drafts):
            if d.content_hash in existing:
                existing[d.content_hash].idx = idx
                if force:
                    existing[d.content_hash].embedding = fresh_by_hash[d.content_hash]
            else:
                session.add(
                    Chunk(document_id=doc.id, idx=idx, content=d.content, content_hash=d.content_hash,
                          meta=d.meta, embedding=fresh_by_hash[d.content_hash])
                )
        doc.title = loaded.title or doc.title
        doc.content_hash = new_hash
        doc.chunks_count = len(drafts)
        doc.status, doc.indexed_at = "indexed", _now()
        await session.commit()
        log.info("indexed document", extra={"event": "index", "doc_id": doc.id})
        return {"status": "indexed", "chunks": len(drafts), "new": len(fresh), "removed": len(stale_ids), "embedded": len(to_embed)}
    except Exception as exc:
        await session.rollback()
        doc = await session.get(Document, document_id)
        if doc is not None:
            doc.status, doc.error = "failed", f"{type(exc).__name__}: {exc}"[:1000]
            await session.commit()
        log.exception("indexing failed doc_id=%s", document_id)
        return {"status": "failed", "error": str(exc)}
