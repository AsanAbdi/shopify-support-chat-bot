"""Mock of the Shopify Admin GraphQL API: a real GraphQL schema/executor (graphql-core), Shopify's query-cost
accounting with a leaky bucket, and the error shapes the real API uses. Not a complete API: the subset a read-only
support bot needs (orders, fulfillments, products, variants, shop).

Behaviours worth knowing (all configurable at runtime via /_debug/shopify/config):
  * 401 for a wrong access token; GraphQL errors come back with HTTP 200
  * single-query cost limit 1000 -> MAX_COST_EXCEEDED; bucket exhausted -> THROTTLED (plan: standard/advanced/plus)
  * orders older than 60 days are invisible unless read_all_orders is "granted"
  * with protected customer data not approved, email/shippingAddress/customer fail with ACCESS_DENIED (partial data)
"""

import base64
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from graphql import (
    GraphQLError,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    build_schema,
    default_field_resolver,
    get_named_type,
    graphql_sync,
    parse,
    validate,
)
from graphql.language import (
    FieldNode,
    FragmentDefinitionNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    VariableNode,
)

from mock_services.shopify_data import STORE_URL, Order, Product, build_orders, build_products, now_utc

router = APIRouter()

SDL = """
scalar DateTime
scalar Money
scalar URL
scalar Decimal
interface Node { id: ID! }

enum CurrencyCode { EUR USD GBP CHF }
enum CountryCode { DE NL BE AT FR LU IT ES }
enum ProductStatus { ACTIVE ARCHIVED DRAFT }
enum OrderDisplayFulfillmentStatus { FULFILLED IN_PROGRESS ON_HOLD OPEN PARTIALLY_FULFILLED PENDING_FULFILLMENT REQUEST_DECLINED RESTOCKED SCHEDULED UNFULFILLED }
enum OrderDisplayFinancialStatus { AUTHORIZED EXPIRED PAID PARTIALLY_PAID PARTIALLY_REFUNDED PENDING REFUNDED VOIDED }
enum OrderReturnStatus { INSPECTION_COMPLETE IN_PROGRESS NO_RETURN RETURNED RETURN_FAILED RETURN_REQUESTED }
enum OrderCancelReason { CUSTOMER DECLINED FRAUD INVENTORY OTHER STAFF }
enum FulfillmentStatus { CANCELLED ERROR FAILURE OPEN PENDING SUCCESS }
enum FulfillmentDisplayStatus { ATTEMPTED_DELIVERY CANCELED CONFIRMED DELAYED DELIVERED FAILURE FULFILLED IN_TRANSIT LABEL_PRINTED LABEL_PURCHASED LABEL_VOIDED MARKED_AS_FULFILLED NOT_DELIVERED OUT_FOR_DELIVERY PICKED_UP READY_FOR_PICKUP SUBMITTED }
enum ProductVariantInventoryPolicy { CONTINUE DENY }
enum OrderSortKeys { CREATED_AT ID ORDER_NUMBER PROCESSED_AT RELEVANCE UPDATED_AT }
enum ProductSortKeys { CREATED_AT ID INVENTORY_TOTAL PRICE PRODUCT_TYPE PUBLISHED_AT RELEVANCE TITLE UPDATED_AT VENDOR }

type PageInfo { hasNextPage: Boolean! hasPreviousPage: Boolean! startCursor: String endCursor: String }
type MoneyV2 { amount: Decimal! currencyCode: CurrencyCode! }
type MoneyBag { shopMoney: MoneyV2! presentmentMoney: MoneyV2! }
type MailingAddress { city: String countryCodeV2: CountryCode country: String zip: String }
type Customer implements Node { id: ID! email: String firstName: String }
type SelectedOption { name: String! value: String! }

type FulfillmentTrackingInfo { company: String number: String url: URL }
type Fulfillment implements Node {
  id: ID! createdAt: DateTime! updatedAt: DateTime! deliveredAt: DateTime inTransitAt: DateTime estimatedDeliveryAt: DateTime
  displayStatus: FulfillmentDisplayStatus status: FulfillmentStatus!
  trackingInfo(first: Int): [FulfillmentTrackingInfo!]!
}
type LineItem implements Node { id: ID! title: String! name: String! quantity: Int! sku: String variantTitle: String }
type LineItemEdge { cursor: String! node: LineItem! }
type LineItemConnection { edges: [LineItemEdge!]! nodes: [LineItem!]! pageInfo: PageInfo! }
type Order implements Node {
  id: ID! name: String! email: String createdAt: DateTime! updatedAt: DateTime! cancelledAt: DateTime cancelReason: OrderCancelReason
  displayFulfillmentStatus: OrderDisplayFulfillmentStatus! displayFinancialStatus: OrderDisplayFinancialStatus
  returnStatus: OrderReturnStatus! confirmed: Boolean! closed: Boolean! test: Boolean!
  currentTotalPriceSet: MoneyBag! shippingAddress: MailingAddress customer: Customer
  fulfillments(first: Int, query: String): [Fulfillment!]!
  lineItems(first: Int, last: Int, after: String, before: String, reverse: Boolean = false): LineItemConnection!
}
type OrderEdge { cursor: String! node: Order! }
type OrderConnection { edges: [OrderEdge!]! nodes: [Order!]! pageInfo: PageInfo! }

type ProductVariant implements Node {
  id: ID! title: String! sku: String price: Money! compareAtPrice: Money availableForSale: Boolean! inventoryQuantity: Int
  inventoryPolicy: ProductVariantInventoryPolicy! selectedOptions: [SelectedOption!]!
}
type ProductVariantEdge { cursor: String! node: ProductVariant! }
type ProductVariantConnection { edges: [ProductVariantEdge!]! nodes: [ProductVariant!]! pageInfo: PageInfo! }
type ProductPriceRangeV2 { minVariantPrice: MoneyV2! maxVariantPrice: MoneyV2! }
type Product implements Node {
  id: ID! title: String! handle: String! description(truncateAt: Int): String! productType: String! vendor: String! status: ProductStatus!
  tags: [String!]! totalInventory: Int! tracksInventory: Boolean! onlineStoreUrl: URL createdAt: DateTime! publishedAt: DateTime
  priceRangeV2: ProductPriceRangeV2!
  variants(first: Int, last: Int, after: String, before: String, reverse: Boolean = false): ProductVariantConnection!
}
type ProductEdge { cursor: String! node: Product! }
type ProductConnection { edges: [ProductEdge!]! nodes: [Product!]! pageInfo: PageInfo! }

type Shop { name: String! myshopifyDomain: String! currencyCode: CurrencyCode! primaryDomain: Domain! }
type Domain { url: URL! host: String! }

type QueryRoot {
  shop: Shop!
  product(id: ID!): Product
  orders(first: Int, last: Int, after: String, before: String, reverse: Boolean = false, sortKey: OrderSortKeys = PROCESSED_AT, query: String): OrderConnection!
  products(first: Int, last: Int, after: String, before: String, reverse: Boolean = false, sortKey: ProductSortKeys = ID, query: String): ProductConnection!
}
schema { query: QueryRoot }
"""

SCHEMA = build_schema(SDL)
MAX_SINGLE_QUERY_COST = 1000
MAX_FIRST = 250
DOC_RATE = "https://shopify.dev/api/usage/rate-limits"
PLANS = {"standard": (1000.0, 100.0), "advanced": (2000.0, 200.0), "plus": (10000.0, 1000.0)}


# ------------------------------------------------------------------ runtime state (reset via debug endpoints)

@dataclass
class State:
    plan: str = os.environ.get("MOCK_SHOPIFY_PLAN", "plus")
    all_orders: bool = os.environ.get("MOCK_SHOPIFY_ALL_ORDERS", "0") == "1"  # read_all_orders granted?
    protected_data: bool = os.environ.get("MOCK_SHOPIFY_PROTECTED_DATA", "1") == "1"  # protected customer data approved?
    fail_next: int = 0
    fail_status: int = 503
    retry_after: int | None = None
    level: float = 0.0
    stamp: float = 0.0
    calls: int = 0

    def __post_init__(self) -> None:
        self.level, self.stamp = PLANS[self.plan][0], time.monotonic()

    def refill(self) -> tuple[float, float, float]:
        cap, rate = PLANS[self.plan]
        now = time.monotonic()
        self.level = min(cap, self.level + (now - self.stamp) * rate)
        self.stamp = now
        return cap, rate, self.level


STATE = State()
_NOW = now_utc()
ORDERS: list[Order] = build_orders(_NOW)
PRODUCTS: list[Product] = build_products(_NOW)


def refresh_if_new_day() -> None:
    """The data is relative to 'now' (shipped yesterday, due today). A mock that keeps running for days would drift into
    nonsense (a parcel 'out for delivery' whose estimate was three days ago), so it is rebuilt when the UTC date changes."""
    global _NOW, ORDERS, PRODUCTS
    now = now_utc()
    if now.date() != _NOW.date():
        _NOW, ORDERS, PRODUCTS = now, build_orders(now), build_products(now)


def reset_state() -> None:
    global STATE
    STATE = State()


# ------------------------------------------------------------------ helpers: cursors, connections, filters

def _cursor(i: int) -> str:
    return base64.b64encode(f"idx:{i}".encode()).decode()


def _uncursor(c: str | None) -> int | None:
    if not c:
        return None
    try:
        return int(base64.b64decode(c).decode().split(":")[1])
    except Exception as exc:  # noqa: BLE001
        raise GraphQLError("Invalid cursor", extensions={"code": "INVALID_CURSOR"}) from exc


def _connection(items: list, field: str, *, first=None, last=None, after=None, before=None, reverse=False, ctx=None) -> dict:
    if first is None and last is None:
        raise GraphQLError("you must provide one of first or last", extensions={"code": "BAD_REQUEST"})
    for n in (first, last):
        if n is not None and n > MAX_FIRST:
            raise GraphQLError(f"Requesting {n} records on the `{field}` connection exceeds the `first` limit of {MAX_FIRST} records.",
                               extensions={"code": "MAX_INPUT_SIZE_EXCEEDED"})
    indexed = list(enumerate(reversed(items) if reverse else items))
    a, b = _uncursor(after), _uncursor(before)
    window = [t for t in indexed if (a is None or t[0] > a) and (b is None or t[0] < b)]
    page = window[:first] if first is not None else window[-last:]
    has_next = first is not None and len(window) > first
    has_prev = (a is not None) or (last is not None and len(window) > last)
    edges = [{"cursor": _cursor(i), "node": node} for i, node in page]
    if ctx is not None:
        ctx["actual"] += 2 + len(page)
    return {"edges": edges, "nodes": [e["node"] for e in edges],
            "pageInfo": {"hasNextPage": has_next, "hasPreviousPage": has_prev,
                         "startCursor": edges[0]["cursor"] if edges else None, "endCursor": edges[-1]["cursor"] if edges else None}}


_TOKEN = re.compile(r'(-?)([a-z_]+):("([^"]*)"|\S+)|("([^"]*)"|\S+)')


def _parse_query(q: str | None) -> list[tuple[bool, str | None, str]]:
    out = []
    for m in _TOKEN.finditer(q or ""):
        if m.group(2):
            out.append((bool(m.group(1)), m.group(2), (m.group(4) if m.group(4) is not None else m.group(3))))
        else:
            out.append((False, None, m.group(6) if m.group(6) is not None else m.group(5)))
    return out


def _cmp_date(value: datetime, expr: str) -> bool:
    m = re.match(r"^(>=|<=|>|<|=)?(.+)$", expr)
    op, raw = m.group(1) or "=", m.group(2)
    try:
        ref = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GraphQLError(f"Invalid timestamp '{raw}'", extensions={"code": "INVALID_SEARCH_QUERY"}) from exc
    ref = ref if ref.tzinfo else ref.replace(tzinfo=UTC)
    return {">=": value >= ref, "<=": value <= ref, ">": value > ref, "<": value < ref, "=": value.date() == ref.date()}[op]


def _like(text: str, pattern: str) -> bool:
    if "*" in pattern:
        return re.fullmatch(".*".join(re.escape(p) for p in pattern.lower().split("*")), text.lower()) is not None
    return text.lower() == pattern.lower()


def _order_matches(o: Order, neg: bool, key: str | None, val: str) -> bool:
    v = val.strip()
    if key is None:
        hit = v.lower() in f"{o.name} {o.email or ''} {' '.join(t for t, *_ in o.lines)}".lower()
    elif key == "name":
        hit = v.lstrip("#") == str(o.number)
    elif key == "email":
        hit = (o.email or "").lower() == v.lower()
    elif key == "financial_status":
        hit = o.financial.lower() == v.lower()
    elif key == "fulfillment_status":
        hit = {"unshipped": o.fulfillment_display in {"UNFULFILLED", "OPEN"}, "shipped": bool(o.fulfillments), "fulfilled": o.fulfillment_display == "FULFILLED",
               "partial": o.fulfillment_display == "PARTIALLY_FULFILLED", "unfulfilled": o.fulfillment_display == "UNFULFILLED",
               "on_hold": o.fulfillment_display == "ON_HOLD"}.get(v.lower(), False)
    elif key == "status":
        hit = {"any": True, "open": not o.closed and not o.cancelled_at, "closed": o.closed, "cancelled": o.cancelled_at is not None}.get(v.lower(), False)
    elif key == "created_at":
        hit = _cmp_date(o.created_at, v)
    elif key == "test":
        hit = o.test == (v.lower() == "true")
    elif key == "id":
        hit = v.split("/")[-1] == str(o.number)
    else:
        raise GraphQLError(f"Invalid search field for orders query: {key}", extensions={"code": "INVALID_SEARCH_FIELD"})
    return hit != neg


def _product_matches(p: Product, neg: bool, key: str | None, val: str) -> bool:
    v = val.strip()
    if key is None:
        hit = v.lower() in f"{p.title} {p.product_type} {p.vendor} {' '.join(p.tags)}".lower()
    elif key == "status":
        hit = p.status.lower() == v.lower()
    elif key == "title":
        hit = _like(p.title, v)
    elif key == "product_type":
        hit = _like(p.product_type, v)
    elif key == "vendor":
        hit = _like(p.vendor, v)
    elif key == "tag":
        hit = v.lower() in (t.lower() for t in p.tags)
    elif key == "sku":
        hit = any(_like(x.sku, v) for x in p.variants)
    elif key == "handle":
        hit = p.handle == v
    elif key == "published_status":
        hit = {"published": p.published, "unpublished": not p.published, "any": True}.get(v.lower(), False)
    elif key == "inventory_total":
        m = re.match(r"^(>=|<=|>|<|=)?(\d+)$", v)
        hit = bool(m) and {">=": p.total_inventory >= int(m.group(2)), "<=": p.total_inventory <= int(m.group(2)), ">": p.total_inventory > int(m.group(2)),
                           "<": p.total_inventory < int(m.group(2)), "=": p.total_inventory == int(m.group(2))}[m.group(1) or "="]
    elif key == "created_at":
        hit = _cmp_date(p.created_at, v)
    else:
        raise GraphQLError(f"Invalid search field for products query: {key}", extensions={"code": "INVALID_SEARCH_FIELD"})
    return hit != neg


# ------------------------------------------------------------------ resolvers

def _money(amount: str) -> dict:
    m = {"amount": amount, "currencyCode": "EUR"}
    return {"shopMoney": m, "presentmentMoney": m}


def _protected(field: str):
    if not STATE.protected_data:
        raise GraphQLError(
            f"This app is not approved to access the {field} field. See https://shopify.dev/docs/apps/launch/protected-customer-data for more details.",
            extensions={"code": "ACCESS_DENIED", "documentation": "https://shopify.dev/api/usage/access-scopes", "requiredAccess": "Protected customer data access"},
        )


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def _order_dict(o: Order) -> dict:
    return {"_o": o}


def _r_orders(_src, info, **a):
    items = list(ORDERS)
    if not STATE.all_orders:  # the real API only exposes the last 60 days without read_all_orders
        cutoff = _NOW - timedelta(days=60)
        items = [o for o in items if o.created_at >= cutoff]
    for neg, key, val in _parse_query(a.get("query")):
        items = [o for o in items if _order_matches(o, neg, key, val)]
    items.sort(key=lambda o: o.created_at)
    return _connection([_order_dict(o) for o in items], "orders", ctx=info.context, **{k: a.get(k) for k in ("first", "last", "after", "before")}, reverse=a.get("reverse", False))


def _r_products(_src, info, **a):
    items = list(PRODUCTS)
    for neg, key, val in _parse_query(a.get("query")):
        items = [p for p in items if _product_matches(p, neg, key, val)]
    return _connection([{"_p": p} for p in items], "products", ctx=info.context, **{k: a.get(k) for k in ("first", "last", "after", "before")}, reverse=a.get("reverse", False))


def _fulfillment_dict(f, idx: int, order: Order) -> dict:
    return {"id": f"gid://shopify/Fulfillment/{order.number}00{idx}", "createdAt": _iso(f.created_at), "updatedAt": _iso(f.delivered_at or f.in_transit_at or f.created_at),
            "deliveredAt": _iso(f.delivered_at), "inTransitAt": _iso(f.in_transit_at), "estimatedDeliveryAt": _iso(f.estimated_delivery_at),
            "displayStatus": f.display_status, "status": f.status,
            "_tracking": [{"company": f.company, "number": f.number, "url": f.url}] if f.number else []}


def _r_order_field(src, info, **a):
    o: Order = src["_o"]
    name = info.field_name
    if name == "id":
        return f"gid://shopify/Order/{4_500_000_000_000 + o.number}"
    if name == "name":
        return o.name
    if name == "email":
        _protected("email of the Order object")
        return o.email
    if name == "customer":
        _protected("Customer object")
        return {"id": f"gid://shopify/Customer/{7_000_000_000 + o.number}", "email": o.email, "firstName": (o.email or "guest").split(".")[0].title()} if o.email else None
    if name == "shippingAddress":
        _protected("shippingAddress field of the Order object")
        return {"city": o.city, "countryCodeV2": o.country, "country": {"DE": "Germany", "NL": "Netherlands", "BE": "Belgium", "AT": "Austria", "FR": "France"}.get(o.country, o.country), "zip": None}
    values = {"createdAt": _iso(o.created_at), "updatedAt": _iso(max([o.created_at] + [f.created_at for f in o.fulfillments])), "cancelledAt": _iso(o.cancelled_at),
              "cancelReason": o.cancel_reason, "displayFulfillmentStatus": o.fulfillment_display, "displayFinancialStatus": o.financial,
              "returnStatus": o.return_status, "confirmed": True, "closed": o.closed, "test": o.test, "currentTotalPriceSet": _money(o.total)}
    if name in values:
        return values[name]
    if name == "fulfillments":
        fs = [_fulfillment_dict(f, i, o) for i, f in enumerate(o.fulfillments)]
        return fs[: a["first"]] if a.get("first") else fs
    if name == "lineItems":
        lines = [{"id": f"gid://shopify/LineItem/{o.number}{i}", "title": t, "name": f"{t} - {v}", "quantity": q, "sku": s, "variantTitle": v} for i, (t, v, q, s) in enumerate(o.lines)]
        return _connection(lines, "lineItems", ctx=info.context, **{k: a.get(k) for k in ("first", "last", "after", "before")}, reverse=a.get("reverse", False))
    return default_field_resolver(src, info, **a)


def _r_product_field(src, info, **a):
    p: Product = src["_p"]
    name = info.field_name
    simple = {"id": p.id, "title": p.title, "handle": p.handle, "productType": p.product_type, "vendor": p.vendor, "status": p.status, "tags": p.tags,
              "totalInventory": p.total_inventory, "tracksInventory": True, "createdAt": _iso(p.created_at), "publishedAt": _iso(p.created_at) if p.published else None,
              "onlineStoreUrl": f"{STORE_URL}/products/{p.handle}" if p.published and p.status == "ACTIVE" else None}
    if name in simple:
        return simple[name]
    if name == "description":
        n = a.get("truncateAt")
        return p.description[:n] if n else p.description
    if name == "priceRangeV2":
        return {"minVariantPrice": {"amount": p.min_price, "currencyCode": "EUR"}, "maxVariantPrice": {"amount": p.max_price, "currencyCode": "EUR"}}
    if name == "variants":
        vs = [{"_v": v} for v in p.variants]
        return _connection(vs, "variants", ctx=info.context, **{k: a.get(k) for k in ("first", "last", "after", "before")}, reverse=a.get("reverse", False))
    return default_field_resolver(src, info, **a)


def _r_variant_field(src, info, **a):
    v = src["_v"]
    m = {"id": v.id, "title": v.title, "sku": v.sku, "price": v.price, "compareAtPrice": v.compare_at, "availableForSale": v.available,
         "inventoryQuantity": v.inventory, "inventoryPolicy": v.policy, "selectedOptions": [{"name": n, "value": val} for n, val in v.options]}
    return m[info.field_name] if info.field_name in m else default_field_resolver(src, info, **a)


def _r_tracking(src, _info, **a):
    t = src["_tracking"]
    return t[: a["first"]] if a.get("first") else t


RESOLVERS = {
    ("QueryRoot", "orders"): _r_orders,
    ("QueryRoot", "products"): _r_products,
    ("QueryRoot", "product"): lambda _s, _i, id, **_k: next(({"_p": p} for p in PRODUCTS if p.id == id), None),
    ("QueryRoot", "shop"): lambda *_a, **_k: {"name": "Nordvik Outdoor", "myshopifyDomain": "nordvik-outdoor.myshopify.com", "currencyCode": "EUR",
                                              "primaryDomain": {"url": STORE_URL, "host": "nordvik-outdoor.example"}},
    ("Fulfillment", "trackingInfo"): _r_tracking,
}
for _f in ("id", "name", "email", "createdAt", "updatedAt", "cancelledAt", "cancelReason", "displayFulfillmentStatus", "displayFinancialStatus", "returnStatus",
           "confirmed", "closed", "test", "currentTotalPriceSet", "shippingAddress", "customer", "fulfillments", "lineItems"):
    RESOLVERS[("Order", _f)] = _r_order_field
for _f in ("id", "title", "handle", "description", "productType", "vendor", "status", "tags", "totalInventory", "tracksInventory", "onlineStoreUrl", "createdAt", "publishedAt",
           "priceRangeV2", "variants"):
    RESOLVERS[("Product", _f)] = _r_product_field
for _f in ("id", "title", "sku", "price", "compareAtPrice", "availableForSale", "inventoryQuantity", "inventoryPolicy", "selectedOptions"):
    RESOLVERS[("ProductVariant", _f)] = _r_variant_field


def _resolver(source, info, **args):
    fn = RESOLVERS.get((info.parent_type.name, info.field_name))
    return fn(source, info, **args) if fn else default_field_resolver(source, info, **args)


# ------------------------------------------------------------------ query cost (Shopify's calculated query cost)

def _arg_int(node: FieldNode, name: str, variables: dict) -> int | None:
    for a in node.arguments or ():
        if a.name.value == name:
            if isinstance(a.value, VariableNode):
                return variables.get(a.value.name.value)
            return int(getattr(a.value, "value", 0))
    return None


def _requested_cost(sel_set, parent: GraphQLObjectType, variables: dict, fragments: dict) -> int:
    total = 0
    for sel in sel_set.selections if sel_set else []:
        if isinstance(sel, InlineFragmentNode):
            total += _requested_cost(sel.selection_set, parent, variables, fragments)
            continue
        if not isinstance(sel, FieldNode):
            frag = fragments.get(sel.name.value)
            total += _requested_cost(frag.selection_set, parent, variables, fragments) if frag else 0
            continue
        if sel.name.value.startswith("__"):
            continue
        fdef = parent.fields.get(sel.name.value)
        if fdef is None:
            continue
        named = get_named_type(fdef.type)
        if not isinstance(named, GraphQLObjectType):
            continue  # scalars and enums are free
        n = _arg_int(sel, "first", variables) or _arg_int(sel, "last", variables)
        if named.name.endswith("Connection"):
            node_type = named.fields["nodes"].type
            node_obj = get_named_type(node_type)
            child = 0
            for inner in sel.selection_set.selections:
                if isinstance(inner, FieldNode) and inner.name.value in {"nodes", "edges"}:
                    target = inner
                    if inner.name.value == "edges":
                        target = next((x for x in inner.selection_set.selections if isinstance(x, FieldNode) and x.name.value == "node"), None)
                    if target is not None:
                        child += _requested_cost(target.selection_set, node_obj, variables, fragments)
            total += 2 + (n or 0) * (1 + child)
        elif isinstance(fdef.type.of_type if isinstance(fdef.type, GraphQLNonNull) else fdef.type, GraphQLList):
            total += (n or 1) * (1 + _requested_cost(sel.selection_set, named, variables, fragments))
        else:
            total += 1 + _requested_cost(sel.selection_set, named, variables, fragments)
    return total


# ------------------------------------------------------------------ HTTP endpoints

def _cost_ext(requested: int, actual: int | None, cap: float, rate: float, level: float) -> dict:
    return {"cost": {"requestedQueryCost": requested, "actualQueryCost": actual,
                     "throttleStatus": {"maximumAvailable": cap, "currentlyAvailable": int(level), "restoreRate": rate}}}


@router.post("/admin/api/{version}/graphql.json")
async def graphql_endpoint(version: str, request: Request, x_shopify_access_token: str | None = Header(default=None)):
    expected = os.environ.get("SHOPIFY_ADMIN_TOKEN", "mock-shopify-token")
    headers = {"X-Request-ID": base64.b16encode(os.urandom(8)).decode().lower(), "X-Shopify-API-Version": version}
    if x_shopify_access_token != expected:
        return JSONResponse({"errors": "[API] Invalid API key or access token (unrecognized login or wrong password)"}, status_code=401, headers=headers)
    STATE.calls += 1
    refresh_if_new_day()
    if STATE.fail_next > 0:  # injected outage / HTTP-level throttling
        STATE.fail_next -= 1
        h = dict(headers)
        if STATE.retry_after is not None:
            h["Retry-After"] = str(STATE.retry_after)
        return JSONResponse({"errors": "Service unavailable" if STATE.fail_status >= 500 else "Throttled"}, status_code=STATE.fail_status, headers=h)

    body = await request.json()
    query, variables = body.get("query") or "", body.get("variables") or {}
    try:
        doc = parse(query)
    except GraphQLError as exc:
        return JSONResponse({"errors": [{"message": exc.message, "locations": [{"line": loc.line, "column": loc.column} for loc in exc.locations or []]}]}, headers=headers)
    problems = validate(SCHEMA, doc)
    if problems:
        errs = []
        for p in problems:
            e = {"message": p.message, "locations": [{"line": loc.line, "column": loc.column} for loc in p.locations or []], "path": ["query"]}
            m = re.match(r"Cannot query field '(\w+)' on type '(\w+)'", p.message)
            e["extensions"] = {"code": "undefinedField", "typeName": m.group(2), "fieldName": m.group(1)} if m else {"code": "argumentLiteralsIncompatible"}
            if m:
                e["message"] = f"Field '{m.group(1)}' doesn't exist on type '{m.group(2)}'"
            errs.append(e)
        return JSONResponse({"errors": errs}, headers=headers)

    fragments = {d.name.value: d for d in doc.definitions if isinstance(d, FragmentDefinitionNode)}
    op = next(d for d in doc.definitions if isinstance(d, OperationDefinitionNode))
    requested = _requested_cost(op.selection_set, SCHEMA.query_type, variables, fragments)
    cap, rate, level = STATE.refill()
    if requested > MAX_SINGLE_QUERY_COST:
        msg = (f"Query cost is {requested}, which exceeds the single query max cost limit ({MAX_SINGLE_QUERY_COST}).\n\n"
               "See https://shopify.dev/concepts/about-apis/rate-limits for more information on how the cost of a query is calculated.\n\n"
               "To query larger amounts of data with fewer limits, bulk operations should be used instead. "
               "See https://shopify.dev/api/usage/bulk-operations/queries for usage details.")
        return JSONResponse({"errors": [{"message": msg, "extensions": {"code": "MAX_COST_EXCEEDED", "cost": requested, "maxCost": MAX_SINGLE_QUERY_COST, "documentation": DOC_RATE}}],
                             "extensions": _cost_ext(requested, None, cap, rate, level)}, headers=headers)
    if requested > level:
        return JSONResponse({"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED", "documentation": DOC_RATE}}],
                             "extensions": _cost_ext(requested, None, cap, rate, level)}, headers=headers)
    STATE.level -= requested

    ctx = {"actual": 0}
    result = graphql_sync(SCHEMA, query, variable_values=variables, context_value=ctx, field_resolver=_resolver, operation_name=body.get("operationName"))
    actual = max(ctx["actual"], 1)
    STATE.level = min(cap, STATE.level + max(requested - actual, 0))
    out: dict = {}
    if result.data is not None:
        out["data"] = result.data
    if result.errors:
        out["errors"] = [{"message": e.message, "locations": [{"line": loc.line, "column": loc.column} for loc in e.locations or []], "path": e.path,
                          **({"extensions": e.extensions} if e.extensions else {})} for e in result.errors]
    out["extensions"] = _cost_ext(requested, actual, cap, rate, STATE.level)
    return JSONResponse(out, headers=headers)


@router.post("/_debug/shopify/config")
async def debug_config(request: Request) -> dict:
    cfg = await request.json()
    if "plan" in cfg:
        STATE.plan = cfg["plan"]
        STATE.level, STATE.stamp = PLANS[cfg["plan"]][0], time.monotonic()
    if "bucket" in cfg or "restore_rate" in cfg:  # custom plan for fast throttling tests
        PLANS["custom"] = (float(cfg.get("bucket", 1000)), float(cfg.get("restore_rate", 100)))
        STATE.plan, STATE.level, STATE.stamp = "custom", PLANS["custom"][0], time.monotonic()
    for k in ("all_orders", "protected_data", "fail_next", "fail_status", "retry_after"):
        if k in cfg:
            setattr(STATE, k, cfg[k])
    return {"plan": STATE.plan, "all_orders": STATE.all_orders, "protected_data": STATE.protected_data, "fail_next": STATE.fail_next}


@router.post("/_debug/shopify/reset")
async def debug_reset() -> dict:
    PLANS.pop("custom", None)
    reset_state()
    return {"ok": True}


@router.get("/_debug/shopify/stats")
async def debug_stats() -> dict:
    return {"calls": STATE.calls, "plan": STATE.plan, "orders": len(ORDERS), "products": len(PRODUCTS), "variants": sum(len(p.variants) for p in PRODUCTS)}
