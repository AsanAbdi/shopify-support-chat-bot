"""Demo data: sample knowledge base + catalog sync from the (mock) Shopify. Run: make seed"""

import asyncio
import json

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.integrations.catalog import sync_catalog
from app.integrations.shopify import ShopifyClient
from app.rag.embeddings import get_embedder
from app.services.seed import seed_demo_kb


async def main() -> None:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    shopify = ShopifyClient()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            print(json.dumps({"kb": await seed_demo_kb(session)}, indent=2))
            print(json.dumps({"catalog": await sync_catalog(session, shopify, get_embedder())}, indent=2))
    finally:
        await shopify.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
