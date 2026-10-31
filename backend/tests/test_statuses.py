"""The model must never be shown raw Shopify codes: whatever it is shown, it repeats to the customer."""

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agent import statuses
from app.agent.prompts import build_system_prompt
from app.agent.statuses import describe_now, human_date, is_past, label
from app.agent.tools import ToolContext, run_get_order_status, run_search_products
from app.config import get_settings
from app.integrations.catalog import sync_catalog
from app.integrations.shopify import ShopifyClient
from app.rag.embeddings import get_embedder
from mock_services.shopify_data import build_orders, now_utc

ALL_CODES = {code for table in statuses._EN.values() for code in table} | {"NO_RETURN"}
SNAKE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z]+)+\b")


def _leaks(text: str) -> list[str]:
    return SNAKE.findall(text) + [c for c in ALL_CODES if re.search(rf"\b{c}\b", text)]


@pytest.fixture
def ctx(sm):
    s = get_settings()
    return ToolContext(s, sm, get_embedder(), ShopifyClient(s))


async def test_no_order_in_the_shop_leaks_a_code_to_the_model(ctx):
    checked = 0
    for order in build_orders(now_utc()):
        if not order.email or order.test:
            continue
        res = await run_get_order_status({"order_number": str(order.number), "email": order.email}, {}, ctx)
        if not res.artifact.get("found"):
            continue  # older than 60 days: invisible without read_all_orders
        assert _leaks(res.content) == [], f"order {order.number}:\n{res.content}"
        checked += 1
    assert checked >= 15


async def test_the_model_gets_a_readable_sentence_per_fact(ctx):
    res = await run_get_order_status({"order_number": "1006", "email": "mark.visser@example.nl"}, {}, ctx)
    lines = res.content.splitlines()
    assert lines[0] == "Order 1006" and lines[1] == "Order status: shipped" and lines[2] == "Payment: paid"
    parcel = next(ln for ln in lines if ln.startswith("Parcel"))
    assert "(PostNL): out for delivery today" in parcel
    assert re.search(r"estimated delivery (today|tomorrow|yesterday|in \d+ days|\d+ days ago), [A-Z][a-z]+day \d{1,2} [A-Z][a-z]+ \d{4}", parcel)
    assert "tracking page https://jouw.postnl.nl/" in parcel and "tracking number 3SABCD" in parcel
    assert lines[-1] == "Items: 1x Summit GTX Hiking Boot (Black / EU 44)"  # product names stay as in the shop


async def test_special_situations_are_phrased_for_humans(ctx):
    cases = {
        ("1010", "peter.bakker@example.nl"): ["Order status: partly shipped (the rest follows in another parcel)"],
        ("1011", "julia.hoffmann@example.de"): ["Parcel 1 of 2 (DHL): delivered", "Parcel 2 of 2 (DHL): delivered"],
        ("1013", "eva.dekker@example.nl"): ["Return: return on its way back to the shop"],
        ("1004", "lukas.meier@example.de"): ["Order status: cancelled", "Payment: refunded", "Why it was cancelled: cancelled at the customer's request"],
        ("1003", "emma.jones@example.com"): ["Order status: being prepared, not shipped yet", "Shipping: not shipped yet"],
        ("1008", "tom.janssen@example.nl"): ["the carrier could not deliver it and will try again"],
        ("1007", "lena.fischer@example.de"): ["delayed"],
    }
    for (number, email), expected in cases.items():
        res = await run_get_order_status({"order_number": number, "email": email}, {}, ctx)
        for phrase in expected:
            assert phrase in res.content, f"{number}: {phrase!r} not in\n{res.content}"


async def test_product_results_have_no_shouting_markers(ctx, session):
    await sync_catalog(session, ctx.shopify, ctx.embedder)
    res = await run_search_products({"query": "Alpine Shell Jacket"}, {}, ctx)
    assert res.artifact["items"]
    assert not re.search(r"\b(IN STOCK|OUT OF STOCK|ON SALE|LOW STOCK)\b", res.content)
    assert "in stock" in res.content and "Link: https://nordvik-outdoor.example/products/" in res.content


def test_label_never_returns_a_raw_code():
    assert label("delivery", "OUT_FOR_DELIVERY") == "out for delivery today"
    assert label("delivery", "OUT_FOR_DELIVERY", "de") == "heute in Zustellung"
    assert label("delivery", "OUT_FOR_DELIVERY", "nl") == "vandaag in bezorging"
    assert label("delivery", "HOVERBOARD_DROP") == "hoverboard drop"  # a status Shopify invents tomorrow
    assert label("return", "NO_RETURN") is None and label("financial", None) is None
    assert label("fulfillment", "PARTIALLY_REFUNDED_AND_SHINY", "de") == "partially refunded and shiny"
    assert label("fulfillment", "ON_HOLD", "de") == "angehalten"
    assert label("fulfillment", "RESTOCKED", "de") == "cancelled, the items went back into stock"  # falls back to English, not to a code


def test_human_dates():
    assert human_date(datetime(2026, 10, 6, 9, 0, tzinfo=UTC)) == "Tuesday 6 October 2026"
    assert human_date(None) is None


def test_relative_words_are_computed_in_code_not_guessed_by_the_model():
    today = date(2026, 10, 21)  # a Wednesday
    d = lambda day: datetime(2026, 10, day, 12, 0, tzinfo=UTC)  # noqa: E731
    assert human_date(d(21), today) == "today, Wednesday 21 October 2026"  # the exact sentence the model got wrong
    assert human_date(d(22), today) == "tomorrow, Thursday 22 October 2026"
    assert human_date(d(20), today) == "yesterday, Tuesday 20 October 2026"
    assert human_date(d(25), today) == "in 4 days, Sunday 25 October 2026"
    assert human_date(d(18), today) == "3 days ago, Sunday 18 October 2026"


def test_the_shop_time_zone_decides_what_today_means():
    ams = ZoneInfo("Europe/Amsterdam")
    late_evening_utc = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)  # already 22 October in Amsterdam (UTC+2)
    assert human_date(late_evening_utc, date(2026, 10, 22), ams) == "today, Thursday 22 October 2026"
    assert human_date(late_evening_utc, date(2026, 10, 22), None) == "yesterday, Wednesday 21 October 2026"  # naive UTC reading is a day off
    assert is_past(late_evening_utc, date(2026, 10, 22), None) and not is_past(late_evening_utc, date(2026, 10, 22), ams)


def test_the_prompt_tells_the_model_what_day_it_is():
    s = get_settings()
    prompt = build_system_prompt("", injection_suspected=False, pending_email=False)
    assert "# Today" in prompt and describe_now(s)[:16] in prompt  # weekday + day + month, e.g. "Wednesday 21 Oc"
    assert "Europe/Amsterdam" in describe_now(s)
    fixed = build_system_prompt("", injection_suspected=False, pending_email=False, today="Monday 2 November 2026, 09:00 (Europe/Amsterdam)")
    assert "It is Monday 2 November 2026, 09:00" in fixed


async def test_a_late_parcel_is_flagged_so_the_model_apologises_instead_of_promising(ctx, monkeypatch):
    from app.agent import tools

    today = datetime.now(UTC) + timedelta(days=4)  # four days later the estimate for order 1006 lies in the past
    monkeypatch.setattr(tools, "shop_clock", lambda settings: (ZoneInfo(settings.shop_timezone), today.astimezone(ZoneInfo(settings.shop_timezone))))
    res = await run_get_order_status({"order_number": "1006", "email": "mark.visser@example.nl"}, {}, ctx)
    parcel = next(ln for ln in res.content.splitlines() if ln.startswith("Parcel"))
    assert "days ago" in parcel and "(that date has passed: the parcel is late)" in parcel
