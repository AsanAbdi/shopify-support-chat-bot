import os

# Configure BEFORE any `app` import: settings are cached on first use.
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ.update(
    APP_ENV="test", LLM_PROVIDER="fake", EMBEDDINGS_PROVIDER="fake", RERANKER="none", JWT_SECRET="test-secret-not-for-prod",
    ADMIN_USERNAME="admin", ADMIN_PASSWORD="admin-test-pass-1", URL_FETCH_ALLOW_PRIVATE="true",
    RETRIEVAL_MIN_SIMILARITY="0.2", PRODUCT_MIN_SIMILARITY="0.1", CORS_ORIGINS='["*"]',
    ALERT_WEBHOOK_URL="http://mock-services:9000/hooks/alert", ALERT_TELEGRAM_BOT_TOKEN="123:test-token",
    ALERT_TELEGRAM_CHAT_ID="42", ALERT_TELEGRAM_API_URL="http://mock-services:9000/telegram",
)

import subprocess  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import redis as sync_redis  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.config import get_settings  # noqa: E402

TEST_URL = os.environ["TEST_DATABASE_URL"]
ADMIN_URL = os.environ["TEST_ADMIN_DATABASE_URL"]
MOCK_BASE = "http://mock-services:9000"
FIXTURE_KB = Path(__file__).parent / "fixtures" / "kb"  # 11 small documents the tests rely on; the demo KB lives in seed/kb


@pytest.fixture(scope="session", autouse=True)
def _database():
    """Fresh database per run; schema built by the real Alembic migrations (so they are tested too)."""
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS app_test WITH (FORCE)")
        conn.execute("CREATE DATABASE app_test")
    res = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    yield


@pytest.fixture(scope="session", autouse=True)
def _redis_clean():
    sync_redis.from_url(get_settings().redis_url).flushdb()


@pytest_asyncio.fixture
async def redis():
    """An async Redis client on an empty database (locks, rate limits and alert flags start from scratch)."""
    r = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    await r.flushdb()
    yield r
    await r.aclose()


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_URL, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sm(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _clean(engine):
    async with engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE chunks, documents, products, messages, conversations, llm_usage, unanswered_questions RESTART IDENTITY CASCADE"
        ))
        for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            if (await conn.execute(text("SELECT to_regclass(:t)"), {"t": tbl})).scalar():
                await conn.execute(text(f"TRUNCATE {tbl}"))  # noqa: S608
    yield


@pytest_asyncio.fixture
async def session(sm):
    async with sm() as s:
        yield s


@pytest_asyncio.fixture
async def seeded_kb(session):
    from app.services.seed import seed_demo_kb

    result = await seed_demo_kb(session, FIXTURE_KB)
    assert all(r["status"] == "indexed" for r in result.values()), result
    return result


@pytest_asyncio.fixture
async def mock_http():
    async with httpx.AsyncClient(base_url=MOCK_BASE, timeout=10) as c:
        yield c


@pytest.fixture
def new_id() -> str:
    return str(uuid.uuid4())
