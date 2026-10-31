import pytest

from app.agent.tools import (
    MAX_ORDER_FAILURES,
    ToolContext,
    run_get_order_status,
    run_search_knowledge_base,
    run_search_products,
)
from app.config import get_settings
from app.integrations.catalog import sync_catalog
from app.integrations.shopify import ShopifyClient
from app.rag.embeddings import get_embedder


@pytest.fixture
def ctx(sm):
    s = get_settings()
    return ToolContext(s, sm, get_embedder(), ShopifyClient(s))


async def test_order_status_with_matching_email(ctx):
    res = await run_get_order_status({"order_number": "#1001", "email": "anna.schmidt@example.de"}, {}, ctx)
    assert res.artifact["found"] and res.artifact["tracking_number"] == "00340434161094042557"
    assert "tracking number 00340434161094042557" in res.content and "(DHL): delivered" in res.content and "Order status: shipped" in res.content
    assert res.update["customer_email"] == "anna.schmidt@example.de"


async def test_email_match_ignores_case_and_whitespace(ctx):
    res = await run_get_order_status({"order_number": "1002", "email": "  Jan.DeVries@Example.NL "}, {}, ctx)
    assert res.artifact["found"]


async def test_wrong_email_leaks_nothing_and_is_indistinguishable_from_unknown_order(ctx):
    wrong_email = await run_get_order_status({"order_number": "1001", "email": "attacker@example.com"}, {}, ctx)
    no_such_order = await run_get_order_status({"order_number": "9999", "email": "attacker@example.com"}, {}, ctx)
    for res in (wrong_email, no_such_order):
        assert res.artifact == {"kind": "order", "found": False}
        assert "00340434161094042557" not in res.content and "DHL" not in res.content and "Trail Runner" not in res.content
        assert res.update == {"order_failures": 1}
    assert wrong_email.content == no_such_order.content


@pytest.mark.parametrize("email", ["", "not-an-email", "anna.schmidt@example.de, other@example.com", "*@example.de"])
async def test_malformed_email_is_rejected_without_calling_shopify(ctx, email):
    res = await run_get_order_status({"order_number": "1001", "email": email}, {}, ctx)
    assert res.artifact["found"] is False


async def test_lookups_lock_after_too_many_failures_even_with_correct_data(ctx):
    res = await run_get_order_status({"order_number": "1001", "email": "anna.schmidt@example.de"}, {"order_failures": MAX_ORDER_FAILURES}, ctx)
    assert "ORDER_LOOKUP_LOCKED" in res.content and not res.artifact["found"]
    assert res.update["pending_escalation"]


async def test_shopify_outage_is_handled_gracefully(sm):
    s = get_settings().model_copy(update={"shopify_base_url": "http://127.0.0.1:9"})
    ctx = ToolContext(s, sm, get_embedder(), ShopifyClient(s))
    res = await run_get_order_status({"order_number": "1001", "email": "anna.schmidt@example.de"}, {}, ctx)
    assert "ORDER_SYSTEM_UNAVAILABLE" in res.content and res.artifact["error"]


async def test_cancelled_order_status(ctx):
    res = await run_get_order_status({"order_number": "1004", "email": "lukas.meier@example.de"}, {}, ctx)
    assert res.artifact["status"] == "CANCELLED"


async def test_catalog_sync_is_incremental_and_searchable(session, ctx):
    stats = await sync_catalog(session, ctx.shopify, ctx.embedder)
    assert stats["upserted"] == stats["embedded"] > 60  # several pages worth of products
    again = await sync_catalog(session, ctx.shopify, ctx.embedder)
    assert again["embedded"] == 0 and again["removed"] == 0  # unchanged products are not re-embedded

    res = await run_search_products({"query": "Alpine Shell Jacket"}, {}, ctx)
    assert res.artifact["items"] and res.artifact["items"][0]["title"].startswith("Alpine Shell Jacket")
    assert res.artifact["items"][0]["url"].startswith("https://nordvik-outdoor.example/products/")

    cheap = await run_search_products({"query": "jacket", "max_price": 100.0, "in_stock_only": True}, {}, ctx)
    assert all(i["price"] <= 100.0 and i["available"] for i in cheap.artifact["items"])


async def test_knowledge_search_returns_numbered_sources(ctx, seeded_kb):
    res = await run_search_knowledge_base({"query": "How long do I have to return an item?"}, {}, ctx)
    assert res.artifact["found"] and res.content.startswith("[1] ")
    assert res.update["turn_sources"][0]["ref"] == "returns.md" and res.update["low_conf_streak"] == 0


async def test_knowledge_search_miss_counts_toward_escalation(ctx, seeded_kb):
    res = await run_search_knowledge_base({"query": "weather in Berlin tomorrow"}, {"low_conf_streak": 1}, ctx)
    assert "NO_RELEVANT_RESULTS" in res.content and res.update["low_conf_streak"] == 2 and res.update["turn_no_answer"]


async def test_forced_catalog_sync_re_embeds_every_product(session, ctx):
    first = await sync_catalog(session, ctx.shopify, ctx.embedder)
    assert (await sync_catalog(session, ctx.shopify, ctx.embedder))["embedded"] == 0  # normal run: unchanged products are skipped
    forced = await sync_catalog(session, ctx.shopify, ctx.embedder, force=True)
    assert forced["embedded"] == forced["upserted"] == first["upserted"]
