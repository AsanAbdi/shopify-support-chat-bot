"""Agent tools. Schemas are what the LLM sees; handlers run server-side with injected dependencies.

Security rule: anything that touches customer data is enforced HERE in code, never delegated to the model.
"""

import hmac
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.guard import EMAIL_RE
from app.agent.state import AgentState
from app.agent.statuses import human_date, is_past, label, shop_clock
from app.config import Settings
from app.integrations import catalog
from app.integrations.errors import IntegrationError
from app.integrations.shopify import normalize_order_number
from app.rag.retriever import is_confident, retrieve

log = logging.getLogger(__name__)

MAX_ORDER_FAILURES = 5


class search_knowledge_base(BaseModel):  # noqa: N801
    """Search the help center and policy documents (shipping, returns, payment, sizing, warranty, how-to).
    Use it for every policy or how-to question. Write the query in the customer's language."""

    query: str = Field(description="Self-contained search query")


class get_order_status(BaseModel):  # noqa: N801
    """Read the status of an order: fulfilment status, tracking number and shipping date.
    Requires BOTH the order number and the email address of the order. Never guess missing values."""

    order_number: str = Field(description="Order number, e.g. 1001")
    email: str = Field(description="Email address used for the order")


class search_products(BaseModel):  # noqa: N801
    """Search the product catalog by free text. Returns price, availability and link."""

    query: str
    max_price: float | None = Field(default=None, description="Maximum price in EUR")
    in_stock_only: bool = False
    product_type: str | None = Field(default=None, description="e.g. Jackets, Boots, Running Shoes, Backpacks, Socks")
    size: str | None = Field(default=None, description="Size the customer wants, e.g. M, XL, 42 or EU 42")
    color: str | None = Field(default=None, description="Colour the customer wants, e.g. black, navy")


class report_no_answer(BaseModel):  # noqa: N801
    """Call this when the help-center excerpts / search results do NOT contain the answer to the customer's question
    (so that the miss is recorded). Takes no arguments. Afterwards tell the customer honestly and offer a colleague."""


class create_ticket(BaseModel):  # noqa: N801
    """Hand the conversation to a human colleague (creates a helpdesk ticket with the full history and a summary)."""

    reason: Literal["human_request", "complaint", "money", "other"]


TOOL_SCHEMAS = [search_knowledge_base, get_order_status, search_products, report_no_answer, create_ticket]


@dataclass
class ToolResult:
    content: str
    artifact: dict | None = None
    update: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolContext:
    settings: Settings
    session_factory: Any
    embedder: Any
    shopify: Any


async def run_search_knowledge_base(args: dict, state: AgentState, ctx: ToolContext) -> ToolResult:
    query = str(args.get("query", ""))[:500]
    async with ctx.session_factory() as session:
        hits = await retrieve(session, ctx.embedder, query, settings=ctx.settings)
    streak = state.get("low_conf_streak", 0)
    if not is_confident(hits, ctx.settings):
        return ToolResult(
            "NO_RELEVANT_RESULTS. The help center has no reliable information on this. Say so and offer a human colleague.",
            {"kind": "kb", "found": False},
            {"low_conf_streak": streak + 1, "turn_no_answer": True},
        )
    text, sources = format_hits(hits, list(state.get("turn_sources", [])))
    return ToolResult(text, {"kind": "kb", "found": True}, {"low_conf_streak": 0, "turn_sources": sources})


def format_hits(hits: list, sources: list[dict], *, pre: bool = False) -> tuple[str, list[dict]]:
    """Numbered excerpts for the model. Numbers are per source document and stay stable within one turn,
    so a prefetched excerpt [1] and a later tool result [1] mean the same document."""
    sources = list(sources)
    numbering = {s["ref"]: s["n"] for s in sources}
    blocks = []
    for h in hits:
        if h.source not in numbering:
            numbering[h.source] = len(numbering) + 1
            sources.append({"n": numbering[h.source], "title": h.title, "ref": h.source, "type": h.source_type,
                            "doc_id": h.document_id, **({"pre": True} if pre else {})})
        blocks.append(f"[{numbering[h.source]}] {h.title}\n{h.content}")
    return "\n\n".join(blocks), sources


async def run_get_order_status(args: dict, state: AgentState, ctx: ToolContext) -> ToolResult:
    failures = state.get("order_failures", 0)
    if failures >= MAX_ORDER_FAILURES:
        return ToolResult(
            "ORDER_LOOKUP_LOCKED. Too many failed attempts. Do not retry; offer a human colleague.",
            {"kind": "order", "found": False},
            {"pending_escalation": state.get("pending_escalation") or "other"},
        )
    number = normalize_order_number(str(args.get("order_number", "")))
    email = str(args.get("email", "")).strip().lower()
    fail = ToolResult(
        "ORDER_NOT_FOUND_OR_EMAIL_MISMATCH. Do not say which one is wrong and do not reveal anything about the order.",
        {"kind": "order", "found": False},
        {"order_failures": failures + 1},
    )
    if not number or not EMAIL_RE.fullmatch(email):
        return fail
    try:
        order = await ctx.shopify.find_order(number)
    except IntegrationError:
        log.exception("order lookup failed")
        return ToolResult("ORDER_SYSTEM_UNAVAILABLE. Apologise and suggest trying again shortly or a human colleague.", {"kind": "order", "error": True})
    # Authorization check in code: constant-time comparison, identical failure for 'no such order' and 'wrong email'.
    if order is None or not hmac.compare_digest(order.email.strip().lower().encode(), email.encode()):
        return fail

    status = "CANCELLED" if order.cancelled else order.fulfillment_status
    # Everything below is plain language on purpose (see statuses.py): the model repeats whatever it is shown.
    lines = [f"Order {order.number}", f"Order status: {label('fulfillment', status)}"]
    if fin := label("financial", order.financial_status):
        lines.append(f"Payment: {fin}")
    if order.cancelled and (why := label("cancel", order.cancel_reason)):
        lines.append(f"Why it was cancelled: {why}")
    if ret := label("return", order.return_status):
        lines.append(f"Return: {ret}")

    def day(d) -> str:
        return d.date().isoformat() if d else "-"

    tz, now = shop_clock(ctx.settings)
    today = now.date()
    shipments = []
    for i, sh in enumerate(order.shipments, 1):
        parcel = f"Parcel {i} of {len(order.shipments)}" if len(order.shipments) > 1 else "Parcel"
        facts = [f"{parcel}{f' ({sh.carrier})' if sh.carrier else ''}: {label('delivery', sh.display_status) or 'shipped'}"]
        if sh.shipped_at:
            facts.append(f"shipped {human_date(sh.shipped_at, today, tz)}")
        if sh.delivered_at:
            facts.append(f"delivered {human_date(sh.delivered_at, today, tz)}")
        elif sh.estimated_delivery_at:
            late = is_past(sh.estimated_delivery_at, today, tz)
            facts.append(f"estimated delivery {human_date(sh.estimated_delivery_at, today, tz)}" + (" (that date has passed: the parcel is late)" if late else ""))
        if sh.number:
            facts.append(f"tracking number {sh.number}")
        if sh.url:
            facts.append(f"tracking page {sh.url}")
        lines.append(", ".join(facts))
        shipments.append({"carrier": sh.carrier, "number": sh.number, "url": sh.url, "status": sh.display_status, "shipped": day(sh.shipped_at),
                          "estimated": day(sh.estimated_delivery_at), "delivered": day(sh.delivered_at)})
    if not order.shipments and not order.cancelled:
        lines.append("Shipping: not shipped yet")
    lines.append("Items: " + "; ".join(f"{q}x {title}" for title, q in order.items))
    artifact = {
        "kind": "order", "found": True, "number": order.number, "status": status, "shipments": shipments,
        "tracking_number": order.tracking_number, "carrier": order.carrier,
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else "",
    }
    return ToolResult("\n".join(lines), artifact, {"customer_email": email, "order_number": order.number, "order_failures": 0})


async def run_search_products(args: dict, state: AgentState, ctx: ToolContext) -> ToolResult:  # noqa: ARG001
    size, color = args.get("size"), args.get("color")
    in_stock_only = bool(args.get("in_stock_only"))
    async with ctx.session_factory() as session:
        products = await catalog.search_products(
            session, ctx.embedder, str(args.get("query", ""))[:300], max_price=args.get("max_price"), in_stock_only=in_stock_only,
            product_type=args.get("product_type"), size=size, color=color, limit=5,
        )
    items, lines = [], []
    for p in products:
        wanted = catalog.matching_variants(p, size=size, color=color, in_stock_only=False)
        in_stock = [v for v in wanted if v.get("available")]
        pool = in_stock if (size or color or in_stock_only) else [v for v in (p.variants or []) if v.get("available")]
        sizes = sorted({v["options"].get("Size", "") for v in pool if v.get("options", {}).get("Size")}, key=_size_key)
        colors = sorted({v["options"].get("Color", "") for v in (p.variants or []) if v.get("options", {}).get("Color")})
        on_sale = any(v.get("compare_at") for v in (p.variants or []))
        low = any(0 < (v.get("qty") or 0) <= 3 for v in in_stock)
        available = bool(in_stock) if (size or color) else p.available
        items.append({"title": p.title, "price": float(p.price or 0), "currency": p.currency, "available": available, "url": p.url,
                      "sizes_in_stock": sizes, "colors": colors, "on_sale": on_sale})
        lines.append(
            f"- {p.title}: from {float(p.price or 0):.2f} {p.currency}{' (on sale)' if on_sale else ''}, {'in stock' if available else 'out of stock'}"
            f"{' in the requested size/colour' if (size or color) else ''}{' (only a few left)' if low else ''}. "
            f"Sizes in stock: {', '.join(sizes) or 'none'}. Colours: {', '.join(colors) or 'n/a'}. Link: {p.url}"
        )
    if not items:
        return ToolResult("NO_PRODUCTS_FOUND.", {"kind": "products", "items": []})
    return ToolResult("\n".join(lines), {"kind": "products", "items": items})


def _size_key(s: str) -> tuple:
    order = ["XS", "S", "M", "L", "XL", "XXL", "3XL"]
    digits = "".join(ch for ch in s if ch.isdigit())
    return (0, int(digits)) if digits and s not in order else (1, order.index(s) if s in order else 99, s)


async def run_report_no_answer(args: dict, state: AgentState, ctx: ToolContext) -> ToolResult:  # noqa: ARG001
    """The model judged that the retrieved excerpts do not answer the question: record the miss (feeds the
    'two misses in a row -> human' rule and the unanswered-questions report) and hide the unused sources."""
    streak = state.get("low_conf_streak", 0)
    already = bool(state.get("turn_no_answer"))  # the search tool may have recorded this miss already this turn
    return ToolResult(
        "NO_ANSWER_RECORDED. Tell the customer in one or two sentences that you could not find this in the help center and ask "
        "whether they would like a colleague to take over. Do not guess.",
        {"kind": "kb", "found": False},
        {"turn_no_answer": True, "low_conf_streak": streak if already else streak + 1, "turn_sources": [], "kb_context": ""},
    )


async def run_create_ticket(args: dict, state: AgentState, ctx: ToolContext) -> ToolResult:  # noqa: ARG001
    reason = args.get("reason") or "other"
    return ToolResult("HANDOVER_INITIATED. The system now takes over; do not write more.", {"kind": "handover"}, {"pending_escalation": reason})


HANDLERS = {
    "search_knowledge_base": run_search_knowledge_base,
    "get_order_status": run_get_order_status,
    "search_products": run_search_products,
    "report_no_answer": run_report_no_answer,
    "create_ticket": run_create_ticket,
}
