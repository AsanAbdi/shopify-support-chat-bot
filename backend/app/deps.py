"""Application container: long-lived clients wired once at startup."""

from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import GraphDeps, build_graph
from app.agent.llm import build_chat_model
from app.agent.tools import ToolContext
from app.config import Settings, get_settings
from app.db import session_factory
from app.integrations.freshdesk import FreshdeskClient
from app.integrations.shopify import ShopifyClient
from app.observability import init_langfuse_env
from app.rag.embeddings import Embedder, get_embedder


@dataclass
class Container:
    settings: Settings
    redis: aioredis.Redis
    embedder: Embedder
    shopify: ShopifyClient
    freshdesk: FreshdeskClient
    pool: AsyncConnectionPool
    graph: Any
    graph_deps: GraphDeps
    langfuse_enabled: bool

    async def aclose(self) -> None:
        await self.pool.close()
        await self.shopify.aclose()
        await self.freshdesk.aclose()
        await self.redis.aclose()


def _psycopg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://")


async def build_container(settings: Settings | None = None) -> Container:
    s = settings or get_settings()
    pool = AsyncConnectionPool(
        _psycopg_dsn(s.database_url), min_size=1, max_size=10, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()

    embedder, shopify, freshdesk = get_embedder(), ShopifyClient(s), FreshdeskClient(s)
    llm = build_chat_model(s)
    summary_llm = build_chat_model(s, summary=True)
    graph_deps = GraphDeps(
        settings=s, llm=llm, summary_llm=summary_llm, freshdesk=freshdesk,
        tool_ctx=ToolContext(settings=s, session_factory=session_factory(), embedder=embedder, shopify=shopify),
    )
    return Container(
        settings=s, redis=aioredis.from_url(s.redis_url, decode_responses=True), embedder=embedder, shopify=shopify,
        freshdesk=freshdesk, pool=pool, graph=build_graph(checkpointer), graph_deps=graph_deps,
        langfuse_enabled=init_langfuse_env(s),
    )
