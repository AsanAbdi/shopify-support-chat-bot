"""The real ShopifyClient against the mock-services container, exercising behaviour the real Shopify API has:
cost limits, throttling, access errors, the 60-day order window, variant pagination, partial data."""

import time

import httpx
import pytest
from pydantic import SecretStr

from app.agent.tools import ToolContext, run_get_order_status, run_search_products
from app.config import get_settings
from app.integrations import shopify as shopify_mod
from app.integrations.errors import IntegrationAccessDenied, IntegrationError
from app.integrations.shopify import ShopifyClient
from app.rag.embeddings import get_embedder
from tests.conftest import MOCK_BASE


@pytest.fixture(autouse=True)
async def _reset_mock(mock_http):
    await mock_http.post("/_debug/shopify/reset")
    yield
    await mock_http.post("/_debug/shopify/reset")


@pytest.fixture
def client() -> ShopifyClient:
    return ShopifyClient(get_settings())


async def _config(mock_http, **cfg) -> None:
    assert (await mock_http.post("/_debug/shopify/config", json=cfg)).status_code == 200


async def test_wrong_token_is_a_401_and_not_retried(mock_http):
    s = get_settings().model_copy(update={"shopify_admin_token": SecretStr("shpat_wrong")})
    before = (await mock_http.get("/_debug/shopify/stats")).json()["calls"]
    with pytest.raises(IntegrationAccessDenied, match="access token"):
        await ShopifyClient(s).find_order("1001")
    assert (await mock_http.get("/_debug/shopify/stats")).json()["calls"] == before  # rejected before counting, no retry storm


async def test_the_old_catalog_query_would_have_been_rejected_by_real_shopify(mock_http):
    """products(first:100) x variants(first:50) costs ~10k points; the real limit is 1000 per query."""
    expensive = shopify_mod.PRODUCTS_QUERY.replace("first: 10", "first: 100")
    with pytest.raises(IntegrationError, match="too expensive"):
        await ShopifyClient(get_settings())._gql(expensive, {"cursor": None})
    r = await mock_http.post("/admin/api/2026-07/graphql.json", headers={"X-Shopify-Access-Token": "mock-shopify-token"},
                             json={"query": expensive.replace("$cursor: String", "$cursor: String"), "variables": {"cursor": None}})
    err = r.json()["errors"][0]
    assert r.status_code == 200 and err["extensions"]["code"] == "MAX_COST_EXCEEDED" and err["extensions"]["maxCost"] == 1000
    assert r.json()["extensions"]["cost"]["actualQueryCost"] is None


async def test_the_production_catalog_query_fits_the_cost_limit(mock_http):
    r = await mock_http.post("/admin/api/2026-07/graphql.json", headers={"X-Shopify-Access-Token": "mock-shopify-token"},
                             json={"query": shopify_mod.PRODUCTS_QUERY, "variables": {"cursor": None}})
    cost = r.json()["extensions"]["cost"]
    assert "errors" not in r.json() and 300 < cost["requestedQueryCost"] < 1000 and cost["actualQueryCost"] < cost["requestedQueryCost"]
    assert set(cost["throttleStatus"]) == {"maximumAvailable", "currentlyAvailable", "restoreRate"}


async def test_unknown_fields_fail_validation_like_the_real_api(mock_http):
    r = await mock_http.post("/admin/api/2026-07/graphql.json", headers={"X-Shopify-Access-Token": "mock-shopify-token"},
                             json={"query": "{ orders(first: 1) { edges { node { nope } } } }"})
    err = r.json()["errors"][0]
    assert err["extensions"] == {"code": "undefinedField", "typeName": "Order", "fieldName": "nope"} and err["message"] == "Field 'nope' doesn't exist on type 'Order'"
    r = await mock_http.post("/admin/api/2026-07/graphql.json", headers={"X-Shopify-Access-Token": "mock-shopify-token"}, json={"query": "{ orders { nodes { id } } }"})
    assert "first or last" in r.json()["errors"][0]["message"]


async def test_throttling_makes_the_client_wait_instead_of_failing(mock_http, client):
    await _config(mock_http, bucket=700, restore_rate=400)  # one big page fits, the second must wait ~0.5 s
    started = time.monotonic()
    pages = [await client._gql(shopify_mod.PRODUCTS_QUERY, {"cursor": c}) for c in (None, None)]
    assert all(p["products"]["edges"] for p in pages)
    stats = (await mock_http.get("/_debug/shopify/stats")).json()
    assert stats["calls"] == 3 and time.monotonic() - started >= 0.3  # 2 pages + 1 THROTTLED retry


async def test_full_catalog_iteration_survives_a_tiny_bucket(mock_http, client):
    await _config(mock_http, bucket=1000, restore_rate=1500)
    products = [p async for p in client.iter_products()]
    skus = sum(len(p.variants) for p in products)
    assert 70 <= len(products) <= 100 and 1400 <= skus <= 1600  # ~1500 SKUs, as in the spec
    titles = {p.title for p in products}
    assert not titles & {"Prototype Shell 2027", "Old Season Parka 2022", "Wholesale Only Tent"}  # draft / archived / unpublished


async def test_products_with_more_than_30_variants_are_completed_via_pagination(client):
    jackets = [p async for p in client.iter_products() if p.title == "Alpine Shell Jacket"]
    assert len(jackets) == 1 and len(jackets[0].variants) == 35
    assert {v["options"]["Size"] for v in jackets[0].variants} >= {"XS", "3XL"}
    assert len({v["sku"] for v in jackets[0].variants}) == 35


async def test_http_5xx_and_429_with_retry_after_are_retried(mock_http, client):
    await _config(mock_http, fail_next=2, fail_status=503)
    assert (await client.find_order("1001")).number == "1001"
    await _config(mock_http, fail_next=1, fail_status=429, retry_after=1)
    started = time.monotonic()
    assert (await client.find_order("1001")).number == "1001" and time.monotonic() - started >= 1.0


async def test_persistent_outage_raises_integration_error(mock_http, client, monkeypatch):
    monkeypatch.setattr(shopify_mod, "MAX_ATTEMPTS", 2)
    await _config(mock_http, fail_next=50, fail_status=503)
    with pytest.raises(IntegrationError, match="unavailable"):
        await client.find_order("1001")


async def test_protected_customer_data_not_approved_is_a_clear_error_not_a_crash(mock_http, client):
    await _config(mock_http, protected_data=False)
    with pytest.raises(IntegrationAccessDenied, match="ACCESS_DENIED"):
        await client.find_order("1001")


async def test_orders_older_than_60_days_need_read_all_orders(mock_http, client):
    assert await client.find_order("950") is None  # exists in the shop, but the app has no read_all_orders
    await _config(mock_http, all_orders=True)
    assert (await client.find_order("950")).number == "950"


async def test_delivery_states_are_reported(client):
    delivered, out_for, delayed = await client.find_order("1005"), await client.find_order("1006"), await client.find_order("1007")
    assert delivered.shipments[0].display_status == "DELIVERED" and delivered.shipments[0].delivered_at is not None
    assert out_for.shipments[0].display_status == "OUT_FOR_DELIVERY" and out_for.shipments[0].estimated_delivery_at is not None
    assert delayed.shipments[0].display_status == "DELAYED"
    split = await client.find_order("1011")
    assert len(split.shipments) == 2 and {s.carrier for s in split.shipments} == {"DHL"} and split.tracking_number


async def test_unshipped_cancelled_returned_orders(client):
    open_order, cancelled, in_return, partial = [await client.find_order(n) for n in ("1003", "1004", "1013", "1010")]
    assert open_order.shipments == [] and open_order.fulfillment_status == "UNFULFILLED"
    assert cancelled.cancelled and cancelled.cancel_reason == "CUSTOMER" and cancelled.financial_status == "REFUNDED"
    assert in_return.return_status == "IN_PROGRESS"
    assert partial.fulfillment_status == "PARTIALLY_FULFILLED" and len(partial.shipments) == 1 and len(partial.items) == 3


async def test_merchant_test_orders_and_guest_orders_are_never_disclosed(sm):
    s = get_settings()
    ctx = ToolContext(s, sm, get_embedder(), ShopifyClient(s))
    assert await ctx.shopify.find_order("1019") is None  # test order
    guest = await run_get_order_status({"order_number": "1018", "email": "anyone@example.com"}, {}, ctx)  # no email on the order
    assert guest.artifact == {"kind": "order", "found": False}
    ok = await run_get_order_status({"order_number": "1006", "email": "mark.visser@example.nl"}, {}, ctx)
    assert "(PostNL): out for delivery today" in ok.content and "estimated delivery" in ok.content


async def test_custom_order_name_format_is_used_in_the_search():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(req.content)["variables"]["query"])
        return httpx.Response(200, json={"data": {"orders": {"edges": []}}})

    s = get_settings().model_copy(update={"shopify_order_name_format": "NV-{number}", "shopify_base_url": "https://s.example"})
    assert await ShopifyClient(s, httpx.AsyncClient(transport=httpx.MockTransport(handler))).find_order("#4711") is None
    assert seen == ['name:"NV-4711"']


async def test_product_search_filters_by_size_and_colour_on_real_variants(session, sm, client):
    from app.integrations.catalog import sync_catalog

    await _config_free()
    await sync_catalog(session, client, get_embedder())
    s = get_settings()
    ctx = ToolContext(s, sm, get_embedder(), client)
    res = await run_search_products({"query": "Alpine Shell Jacket", "size": "3XL", "color": "black", "in_stock_only": True}, {}, ctx)
    assert res.artifact["items"]
    for item in res.artifact["items"]:
        assert "3XL" in item["sizes_in_stock"] and item["available"]
    boots = await run_search_products({"query": "Summit GTX Hiking Boot", "size": "EU 44"}, {}, ctx)
    assert boots.artifact["items"] and "EU 44" in boots.content


async def _config_free() -> None:
    async with httpx.AsyncClient(base_url=MOCK_BASE) as c:
        await c.post("/_debug/shopify/config", json={"plan": "plus"})
