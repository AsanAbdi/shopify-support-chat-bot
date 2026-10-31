"""The Shopify client against a transport that records requests: guards the wire contract with the real API."""

import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import get_settings
from app.integrations.errors import IntegrationError
from app.integrations.freshdesk import FreshdeskClient
from app.integrations.shopify import ShopifyClient, normalize_order_number
from app.rag.loaders import fetch_url


def _client(handler) -> ShopifyClient:
    s = get_settings().model_copy(update={"shopify_base_url": "https://shop.example", "shopify_admin_token": SecretStr("shpat_x")})
    return ShopifyClient(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


ORDER = {"data": {"orders": {"edges": [{"node": {
    "id": "gid://shopify/Order/1", "name": "#1001", "email": "Anna@Example.de", "createdAt": "2026-09-25T10:00:00Z",
    "displayFulfillmentStatus": "FULFILLED", "displayFinancialStatus": "PAID", "cancelledAt": None,
    "lineItems": {"edges": [{"node": {"title": "Shoe", "quantity": 1}}]},
    "fulfillments": [{"createdAt": "2026-09-28T09:00:00Z", "status": "SUCCESS",
                      "trackingInfo": [{"number": "T123", "url": "https://t/T123", "company": "DHL"}]}],
}}]}}}


async def test_request_shape_matches_the_admin_graphql_api():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=ORDER)

    order = await _client(handler).find_order("Order #1001")
    req = seen[0]
    assert req.url.path == f"/admin/api/{get_settings().shopify_api_version}/graphql.json"
    assert req.headers["X-Shopify-Access-Token"] == "shpat_x"
    body = json.loads(req.content)
    assert body["variables"] == {"query": 'name:"#1001"'} and "orders(first: 1" in body["query"]
    assert order.number == "1001" and order.tracking_number == "T123" and order.email == "Anna@Example.de"


async def test_throttling_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", lambda *_: _noop())
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 3 else httpx.Response(200, json=ORDER)

    assert (await _client(handler).find_order("1001")).number == "1001" and calls["n"] == 3


async def _noop() -> None:
    return None


async def test_graphql_errors_and_outages_raise_integration_error(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", lambda *_: _noop())
    with pytest.raises(IntegrationError):
        await _client(lambda _r: httpx.Response(200, json={"errors": [{"message": "Throttled"}]})).find_order("1001")
    with pytest.raises(IntegrationError):
        await _client(lambda _r: httpx.Response(503)).find_order("1001")


async def test_unknown_order_returns_none_and_garbage_numbers_never_hit_the_api():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orders": {"edges": []}}})

    client = _client(handler)
    assert await client.find_order("9999") is None
    boom = _client(lambda _r: pytest.fail("API must not be called"))
    assert await boom.find_order("no digits here") is None


def test_order_number_normalisation():
    assert normalize_order_number("#1001") == "1001" and normalize_order_number("Order nr. 20045") == "20045"
    assert normalize_order_number("") == ""


async def test_catalog_pagination_and_inactive_products_are_skipped():
    pages = {
        None: {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "edges": [_product("1", "ACTIVE"), _product("2", "DRAFT")]},
        "c1": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "edges": [_product("3", "ACTIVE")]},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"products": pages[json.loads(req.content)["variables"]["cursor"]]}})

    ids = [p.id async for p in _client(handler).iter_products()]
    assert ids == ["gid://shopify/Product/1", "gid://shopify/Product/3"]


def _product(i: str, status: str) -> dict:
    return {"node": {
        "id": f"gid://shopify/Product/{i}", "title": f"P{i}", "handle": f"p{i}", "description": "d", "productType": "Shoes",
        "vendor": "v", "tags": ["t"], "status": status, "totalInventory": 1, "onlineStoreUrl": f"https://shop.example/products/p{i}",
        "priceRangeV2": {"minVariantPrice": {"amount": "10.00", "currencyCode": "EUR"}},
        "variants": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "edges": [{"node": {"id": "v", "title": "42", "sku": "s", "price": "10.00", "compareAtPrice": None, "availableForSale": True, "inventoryQuantity": 2, "selectedOptions": [{"name": "Size", "value": "42"}]}}]},
    }}


async def test_freshdesk_ticket_payload_and_auth():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"id": 77})

    s = get_settings().model_copy(update={"freshdesk_base_url": "https://acme.freshdesk.com", "freshdesk_api_key": SecretStr("KEY")})
    fd = FreshdeskClient(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    tid = await fd.create_ticket(
        email="a@b.de", subject="s" * 400, summary="line1\nline2 <script>", history=[("user", "hi <b>"), ("assistant", "yo")],
        lang="de", order_number="1001", reason="money", urgent=True,
    )
    req, body = seen[0], json.loads(seen[0].content)
    assert tid == "77" and req.url.path == "/api/v2/tickets"
    assert req.headers["authorization"].startswith("Basic ")  # api key as username, "X" as password
    assert len(body["subject"]) == 250 and body["priority"] == 3 and body["status"] == 2 and body["source"] == 7
    assert "<script>" not in body["description"] and "&lt;script&gt;" in body["description"]  # customer text is escaped
    assert "lang-de" in body["tags"] and "reason-money" in body["tags"]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://localhost/x", "http://10.0.0.5/a", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data",
    "http://[::1]/", "file:///etc/passwd", "ftp://example.com/x",
])
def test_ssrf_guard_blocks_internal_and_non_http_urls(url):
    with pytest.raises(ValueError, match="public|http"):
        fetch_url(url)


def test_ssrf_guard_allows_public_addresses_up_to_the_network_call(monkeypatch):
    class Boom(Exception):
        pass

    def fake_get(*_a, **_k):
        raise Boom

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(Boom):  # got past the guard and attempted the request
        fetch_url("http://93.184.216.34/page")
