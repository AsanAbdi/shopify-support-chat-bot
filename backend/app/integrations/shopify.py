"""Shopify Admin GraphQL client (read-only). Works against Shopify or the mock-services container.

Written against the documented behaviour of the real API:
  * GraphQL errors come back with HTTP 200; THROTTLED means "wait for the bucket", not "fail"
  * a single query may not cost more than 1000 points, so catalog pages are small and variants are paginated
  * order email/address need approved protected-customer-data access (ACCESS_DENIED otherwise)
  * without read_all_orders only the last 60 days of orders are visible
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.config import Settings, get_settings
from app.integrations.errors import IntegrationAccessDenied, IntegrationError

log = logging.getLogger(__name__)

ORDER_QUERY = """
query OrderByName($query: String!) {
  orders(first: 1, query: $query) {
    edges { node {
      id name email createdAt cancelledAt cancelReason test
      displayFulfillmentStatus displayFinancialStatus returnStatus
      lineItems(first: 20) { edges { node { title variantTitle quantity } } }
      fulfillments(first: 10) {
        createdAt status displayStatus inTransitAt deliveredAt estimatedDeliveryAt
        trackingInfo(first: 5) { company number url }
      }
    } }
  }
}
"""

# Cost: 2 + 10 * (1 + 2 + variants(2 + 30 * 2)) = 652 < 1000. Products with more than 30 variants are completed by VARIANTS_QUERY.
PRODUCTS_QUERY = """
query Products($cursor: String) {
  products(first: 10, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id title handle description productType vendor tags status totalInventory onlineStoreUrl
      priceRangeV2 { minVariantPrice { amount currencyCode } }
      variants(first: 30) {
        pageInfo { hasNextPage endCursor }
        edges { node { id title sku price compareAtPrice availableForSale inventoryQuantity selectedOptions { name value } } }
      }
    } }
  }
}
"""

VARIANTS_QUERY = """
query MoreVariants($id: ID!, $cursor: String) {
  product(id: $id) {
    variants(first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      edges { node { id title sku price compareAtPrice availableForSale inventoryQuantity selectedOptions { name value } } }
    }
  }
}
"""

MAX_ATTEMPTS = 6


@dataclass
class Shipment:
    carrier: str | None
    number: str | None
    url: str | None
    display_status: str | None  # IN_TRANSIT, DELIVERED, OUT_FOR_DELIVERY, ATTEMPTED_DELIVERY, DELAYED, READY_FOR_PICKUP, ...
    shipped_at: datetime | None
    in_transit_at: datetime | None = None
    delivered_at: datetime | None = None
    estimated_delivery_at: datetime | None = None


@dataclass
class OrderInfo:
    number: str
    email: str
    created_at: datetime | None
    fulfillment_status: str
    financial_status: str
    cancelled: bool
    cancel_reason: str | None = None
    return_status: str = "NO_RETURN"
    items: list[tuple[str, int]] = field(default_factory=list)
    shipments: list[Shipment] = field(default_factory=list)

    # first-shipment conveniences (most orders have one)
    @property
    def tracking_number(self) -> str | None:
        return next((s.number for s in self.shipments if s.number), None)

    @property
    def tracking_url(self) -> str | None:
        return next((s.url for s in self.shipments if s.number), None)

    @property
    def carrier(self) -> str | None:
        return next((s.carrier for s in self.shipments if s.number), None)

    @property
    def shipped_at(self) -> datetime | None:
        return self.shipments[0].shipped_at if self.shipments else None


@dataclass
class ProductInfo:
    id: str
    title: str
    handle: str
    description: str
    product_type: str
    vendor: str
    price: float | None
    currency: str
    available: bool
    url: str
    variants: list[dict]
    tags: list[str] = field(default_factory=list)


def normalize_order_number(raw: str) -> str:
    m = re.search(r"\d{3,}", raw or "")
    return m.group(0) if m else ""


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _variant(n: dict) -> dict:
    options = {o["name"]: o["value"] for o in n.get("selectedOptions") or [] if o["name"] != "Title"}
    qty = n.get("inventoryQuantity")
    return {
        "title": n["title"], "sku": n.get("sku"), "price": n["price"], "compare_at": n.get("compareAtPrice"),
        "available": bool(n["availableForSale"]), "qty": qty, "options": options,
    }


class ShopifyClient:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.s = settings or get_settings()
        self._client = client or httpx.AsyncClient(timeout=self.s.integration_timeout_s)
        self._url = f"{self.s.shopify_base_url}/admin/api/{self.s.shopify_api_version}/graphql.json"

    async def _gql(self, query: str, variables: dict) -> dict:
        headers = {"X-Shopify-Access-Token": self.s.shopify_admin_token.get_secret_value()}
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            delay = 0.4 * (attempt + 1)
            try:
                resp = await self._client.post(self._url, json={"query": query, "variables": variables}, headers=headers)
            except httpx.TransportError as exc:
                last = exc
                await asyncio.sleep(delay)
                continue
            if resp.status_code in (401, 403):
                raise IntegrationAccessDenied(f"Shopify rejected the access token (HTTP {resp.status_code}); check SHOPIFY_ADMIN_TOKEN and app scopes")
            if resp.status_code in (429, 500, 502, 503, 504):
                last = IntegrationError(f"Shopify HTTP {resp.status_code}")
                await asyncio.sleep(min(float(resp.headers.get("Retry-After", delay)), 20.0))
                continue
            if resp.status_code >= 400:
                raise IntegrationError(f"Shopify HTTP {resp.status_code}")
            body = resp.json()
            errors = body.get("errors")
            if isinstance(errors, list) and errors:
                codes = {(e.get("extensions") or {}).get("code") for e in errors}
                if "THROTTLED" in codes:  # leaky bucket: wait exactly as long as the API says it needs
                    cost = (body.get("extensions") or {}).get("cost") or {}
                    ts = cost.get("throttleStatus") or {}
                    need = float(cost.get("requestedQueryCost") or 0) - float(ts.get("currentlyAvailable") or 0)
                    wait = min(max(need / float(ts.get("restoreRate") or 50), 0.25), 15.0)
                    last = IntegrationError("Shopify throttled")
                    log.info("shopify throttled, waiting %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue
                if "ACCESS_DENIED" in codes:
                    raise IntegrationAccessDenied(f"Shopify ACCESS_DENIED: {errors[0].get('message')}")
                if "MAX_COST_EXCEEDED" in codes:
                    raise IntegrationError(f"Shopify query too expensive (a bug in the query, not retryable): {errors[0].get('message', '')[:120]}")
                raise IntegrationError(f"Shopify GraphQL errors: {[e.get('message') for e in errors][:3]}")
            if isinstance(errors, str):
                raise IntegrationError(f"Shopify error: {errors}")
            return body["data"]
        raise IntegrationError(f"Shopify unavailable: {last}")

    async def find_order(self, order_number: str) -> OrderInfo | None:
        number = normalize_order_number(order_number)
        if not number:
            return None
        name = self.s.shopify_order_name_format.format(number=number)
        data = await self._gql(ORDER_QUERY, {"query": f'name:"{name}"'})
        edges = data["orders"]["edges"]
        if not edges:
            return None
        n = edges[0]["node"]
        if n.get("test"):  # merchant test orders never belong to a customer
            return None
        shipments = [
            Shipment(
                carrier=(t := (f.get("trackingInfo") or [{}])[0]).get("company"), number=t.get("number"), url=t.get("url"),
                display_status=f.get("displayStatus"), shipped_at=_dt(f["createdAt"]), in_transit_at=_dt(f.get("inTransitAt")),
                delivered_at=_dt(f.get("deliveredAt")), estimated_delivery_at=_dt(f.get("estimatedDeliveryAt")),
            )
            for f in sorted(n.get("fulfillments") or [], key=lambda f: f["createdAt"])
            if f.get("status") not in {"CANCELLED", "ERROR", "FAILURE"}
        ]
        return OrderInfo(
            number=n["name"].lstrip("#"), email=(n.get("email") or "").strip(), created_at=_dt(n.get("createdAt")),
            fulfillment_status=n.get("displayFulfillmentStatus") or "UNFULFILLED", financial_status=n.get("displayFinancialStatus") or "",
            cancelled=bool(n.get("cancelledAt")), cancel_reason=n.get("cancelReason"), return_status=n.get("returnStatus") or "NO_RETURN",
            items=[(e["node"]["title"] + (f" ({e['node']['variantTitle']})" if e["node"].get("variantTitle") else ""), e["node"]["quantity"])
                   for e in n["lineItems"]["edges"]],
            shipments=shipments,
        )

    async def _remaining_variants(self, product_id: str, cursor: str | None) -> list[dict]:
        out: list[dict] = []
        while True:
            data = (await self._gql(VARIANTS_QUERY, {"id": product_id, "cursor": cursor}))["product"]["variants"]
            out += [_variant(e["node"]) for e in data["edges"]]
            if not data["pageInfo"]["hasNextPage"]:
                return out
            cursor = data["pageInfo"]["endCursor"]

    async def iter_products(self) -> AsyncIterator[ProductInfo]:
        cursor: str | None = None
        while True:
            data = (await self._gql(PRODUCTS_QUERY, {"cursor": cursor}))["products"]
            for edge in data["edges"]:
                n = edge["node"]
                if n.get("status") not in (None, "ACTIVE") or not n.get("onlineStoreUrl"):
                    continue  # drafts/archived, or not published to the Online Store (no link to give the customer)
                variants = [_variant(v["node"]) for v in n["variants"]["edges"]]
                if n["variants"]["pageInfo"]["hasNextPage"]:
                    variants += await self._remaining_variants(n["id"], n["variants"]["pageInfo"]["endCursor"])
                price = n["priceRangeV2"]["minVariantPrice"]
                yield ProductInfo(
                    id=n["id"], title=n["title"], handle=n["handle"], description=(n.get("description") or "")[:2000],
                    product_type=n.get("productType") or "", vendor=n.get("vendor") or "", tags=n.get("tags") or [],
                    price=float(price["amount"]), currency=price["currencyCode"],
                    available=any(v["available"] for v in variants), url=n["onlineStoreUrl"], variants=variants,
                )
            if not data["pageInfo"]["hasNextPage"]:
                return
            cursor = data["pageInfo"]["endCursor"]

    async def aclose(self) -> None:
        await self._client.aclose()
