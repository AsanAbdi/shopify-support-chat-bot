"""Loads a directory of documents through the normal indexer (demo knowledge base, or test fixtures)."""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document
from app.rag.indexer import index_document

SEED_DIR = Path(__file__).resolve().parents[2] / "seed" / "kb"
SUPPORTED = {".md", ".csv", ".pdf", ".docx", ".html", ".txt"}


async def seed_demo_kb(session: AsyncSession, directory: Path | None = None) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for path in sorted((directory or SEED_DIR).iterdir()):
        if path.suffix.lower() not in SUPPORTED:
            continue
        doc = (await session.execute(select(Document).where(Document.source_ref == path.name))).scalar_one_or_none()
        if doc is None:
            doc = Document(title=path.stem, source_type="file", source_ref=path.name, stored_path=str(path), status="pending")
            session.add(doc)
            await session.commit()
        results[path.name] = await index_document(session, doc.id)
    return results
