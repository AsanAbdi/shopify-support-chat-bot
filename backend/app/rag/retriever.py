"""Hybrid retrieval: pgvector cosine + Postgres full-text, fused with RRF, optional cross-encoder rerank."""

import asyncio
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.rag.embeddings import Embedder
from app.rag.stopwords import STOPWORDS


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    title: str
    source: str
    source_type: str
    content: str
    score: float  # fused RRF (or rerank) score
    similarity: float  # cosine similarity of the vector side, the confidence signal without a reranker
    rerank: float | None = None  # cross-encoder relevance in 0..1, the confidence signal when a reranker is on
    meta: dict = field(default_factory=dict)


def build_tsquery(query: str) -> str:
    toks = [t for t in re.findall(r"\w{3,}", query.lower()) if t not in STOPWORDS]
    return " | ".join(dict.fromkeys(toks))


_HYBRID_SQL = """
WITH vec AS (
    SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> CAST(:qvec AS vector)) AS rnk
    FROM chunks c
    ORDER BY c.embedding <=> CAST(:qvec AS vector)
    LIMIT :n
), fts AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS rnk
    FROM chunks c, to_tsquery('simple', CAST(:tsq AS text)) q
    WHERE CAST(:tsq AS text) <> '' AND c.tsv @@ q
    ORDER BY ts_rank_cd(c.tsv, q) DESC
    LIMIT :n
)
SELECT c.id, c.document_id, d.title, d.source_ref, d.source_type, c.content, c.meta,
       COALESCE(1.0 / (60 + vec.rnk), 0) + COALESCE(1.0 / (60 + fts.rnk), 0) AS score,
       1 - (c.embedding <=> CAST(:qvec AS vector)) AS sim
FROM chunks c
JOIN documents d ON d.id = c.document_id
LEFT JOIN vec ON vec.id = c.id
LEFT JOIN fts ON fts.id = c.id
WHERE vec.id IS NOT NULL OR fts.id IS NOT NULL
ORDER BY score DESC
LIMIT :k
"""


class Reranker:
    def __init__(self, model: str) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._model = TextCrossEncoder(model)

    def _score(self, query: str, docs: list[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, docs)]

    async def rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        scores = await asyncio.to_thread(self._score, query, [h.content for h in hits])
        for h, s in zip(hits, scores, strict=True):
            h.score, h.rerank = s, 1 / (1 + math.exp(-s))
        return sorted(hits, key=lambda h: h.score, reverse=True)


@lru_cache
def _get_reranker(model: str) -> Reranker:
    return Reranker(model)


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


async def retrieve(
    session: AsyncSession, embedder: Embedder, query: str, *, settings: Settings | None = None
) -> list[Hit]:
    settings = settings or get_settings()
    qvec = await embedder.embed_query(query)
    rows = (
        await session.execute(
            text(_HYBRID_SQL),
            {
                "qvec": vector_literal(qvec),
                "tsq": build_tsquery(query),
                "n": settings.retrieval_candidates,
                "k": settings.retrieval_candidates if settings.reranker != "none" else settings.retrieval_top_k,
            },
        )
    ).mappings().all()
    hits = [
        Hit(r["id"], r["document_id"], r["title"], r["source_ref"], r["source_type"], r["content"],
            float(r["score"]), float(r["sim"]), meta=r["meta"] or {})
        for r in rows
    ]
    if settings.reranker != "none" and hits:
        hits = (await _get_reranker(settings.reranker_model).rerank(query, hits))[: settings.retrieval_top_k]
    return hits


def confidence(hits: list[Hit]) -> float:
    if hits and hits[0].rerank is not None:
        return max(h.rerank or 0.0 for h in hits)
    return max((h.similarity for h in hits), default=0.0)


def is_confident(hits: list[Hit], settings: Settings) -> bool:
    """The gate in front of the LLM: below it the bot says 'not in the help center' instead of guessing."""
    threshold = settings.retrieval_min_rerank if settings.reranker != "none" else settings.retrieval_min_similarity
    return bool(hits) and confidence(hits) >= threshold
