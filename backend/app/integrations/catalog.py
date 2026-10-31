"""Daily catalog sync (Shopify -> products table with embeddings) and product search."""

import hashlib
import logging
import re
from decimal import Decimal

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.integrations.shopify import ProductInfo, ShopifyClient
from app.models import Product, _now
from app.rag.embeddings import Embedder
from app.rag.retriever import build_tsquery, vector_literal

log = logging.getLogger(__name__)


def _option_values(variants: list[dict], name: str) -> list[str]:
    seen: dict[str, None] = {}
    for v in variants:
        if val := (v.get("options") or {}).get(name):
            seen[val] = None
    return list(seen)


def product_text(p: ProductInfo) -> str:
    """What gets embedded: title, type, tags, description AND colours/sizes, so 'black jacket size M' can match."""
    return (f"{p.title}. {p.product_type}. {p.vendor}. {' '.join(p.tags)}. {p.description} "
            f"Colors: {', '.join(_option_values(p.variants, 'Color'))}. Sizes: {', '.join(_option_values(p.variants, 'Size'))}")


def embed_hash(p: ProductInfo) -> str:
    return hashlib.md5(product_text(p).encode(), usedforsecurity=False).hexdigest()


async def sync_catalog(session: AsyncSession, shopify: ShopifyClient, embedder: Embedder, *, force: bool = False) -> dict:
    """Upsert every active product; remove the ones that disappeared. Only products whose text changed are re-embedded
    (prices and stock change constantly and must not cost embedding time). `force` re-embeds everything (model change)."""
    existing_hash = {pid: h for pid, h in (await session.execute(text("SELECT id, embed_hash FROM products")))}
    seen: set[str] = set()
    batch: list[ProductInfo] = []
    stats = {"upserted": 0, "embedded": 0, "removed": 0}

    async def flush() -> None:
        if not batch:
            return
        changed = [p for p in batch if force or existing_hash.get(p.id) != embed_hash(p)]
        vecs = await embedder.embed_documents([product_text(p) for p in changed]) if changed else []
        vec_by_id = {p.id: v for p, v in zip(changed, vecs, strict=True)}
        for p in batch:
            row = await session.get(Product, p.id)
            if row is None:
                row = Product(id=p.id, embedding=vec_by_id[p.id], embed_hash=embed_hash(p))
                session.add(row)
            elif p.id in vec_by_id:
                row.embedding, row.embed_hash = vec_by_id[p.id], embed_hash(p)
            row.title, row.handle, row.description, row.tags = p.title, p.handle, p.description, p.tags
            row.product_type, row.vendor, row.price = p.product_type, p.vendor, Decimal(str(p.price))
            row.currency, row.available, row.url, row.variants = p.currency, p.available, p.url, p.variants
            row.synced_at = _now()
        stats["upserted"] += len(batch)
        stats["embedded"] += len(changed)
        await session.commit()
        batch.clear()

    async for product in shopify.iter_products():
        seen.add(product.id)
        batch.append(product)
        if len(batch) >= 50:
            await flush()
    await flush()

    if seen:  # never wipe the catalog because of an empty/failed response
        res = await session.execute(delete(Product).where(Product.id.not_in(seen)))
        stats["removed"] = res.rowcount or 0
        await session.commit()
    log.info("catalog synced %s", stats, extra={"event": "catalog_sync"})
    return stats


_SEARCH_SQL = """
WITH vec AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:qvec AS vector)) AS rnk
    FROM products
    WHERE (CAST(:max_price AS numeric) IS NULL OR price <= CAST(:max_price AS numeric))
      AND (NOT :in_stock OR available)
      AND (CAST(:ptype AS text) IS NULL OR product_type ILIKE CAST(:ptype AS text))
    ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT 40
), fts AS (
    SELECT p.id, row_number() OVER (ORDER BY ts_rank_cd(p.tsv, q) DESC) AS rnk
    FROM products p, to_tsquery('simple', CAST(:tsq AS text)) q
    WHERE CAST(:tsq AS text) <> '' AND p.tsv @@ q
      AND (CAST(:max_price AS numeric) IS NULL OR p.price <= CAST(:max_price AS numeric))
      AND (NOT :in_stock OR p.available)
      AND (CAST(:ptype AS text) IS NULL OR p.product_type ILIKE CAST(:ptype AS text))
    LIMIT 40
)
SELECT p.id, 1 - (p.embedding <=> CAST(:qvec AS vector)) AS sim,
       COALESCE(1.0/(60+vec.rnk),0) + COALESCE(1.0/(60+fts.rnk),0) AS score
FROM products p
LEFT JOIN vec ON vec.id = p.id LEFT JOIN fts ON fts.id = p.id
WHERE vec.id IS NOT NULL OR fts.id IS NOT NULL
ORDER BY score DESC LIMIT :limit
"""


def _norm_size(s: str) -> str:
    return re.sub(r"^eu\s*", "", s.strip().lower())


def matching_variants(p: Product, *, size: str | None, color: str | None, in_stock_only: bool) -> list[dict]:
    out = []
    for v in p.variants or []:
        opts = v.get("options") or {}
        if size and _norm_size(opts.get("Size", "")) != _norm_size(size):
            continue
        if color and color.strip().lower() not in opts.get("Color", "").lower():
            continue
        if in_stock_only and not v.get("available"):
            continue
        out.append(v)
    return out


async def search_products(
    session: AsyncSession, embedder: Embedder, query: str, *, max_price: float | None = None,
    in_stock_only: bool = False, product_type: str | None = None, size: str | None = None, color: str | None = None, limit: int = 5,
) -> list[Product]:
    qvec = await embedder.embed_query(query)
    wide = limit * 6 if (size or color) else limit  # size/colour are checked on variants after retrieval
    rows = (
        await session.execute(
            text(_SEARCH_SQL),
            {"qvec": vector_literal(qvec), "tsq": build_tsquery(query), "max_price": max_price,
             "in_stock": in_stock_only, "ptype": product_type, "limit": wide},
        )
    ).mappings().all()
    min_sim = get_settings().product_min_similarity
    ids = [r["id"] for r in rows if r["sim"] >= min_sim]
    if not ids:
        return []
    products = {p.id: p for p in (await session.execute(select(Product).where(Product.id.in_(ids)))).scalars()}
    ordered = [products[i] for i in ids if i in products]
    if size or color:
        ordered = [p for p in ordered if matching_variants(p, size=size, color=color, in_stock_only=in_stock_only)]
    return ordered[:limit]


__all__ = ["matching_variants", "product_text", "search_products", "sync_catalog"]
