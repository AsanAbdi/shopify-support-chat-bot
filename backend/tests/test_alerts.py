import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text

from app.config import get_settings
from app.logging_conf import mask_deep
from app.observability import _scrub_event
from app.services.alerts import send_alert
from app.services.budget import check_and_alert
from tests.conftest import MOCK_BASE


@pytest.fixture
async def clean_alerts(mock_http):
    r = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    for key in await r.keys("budget-alert:*"):
        await r.delete(key)
    await mock_http.delete("/hooks/_debug")
    yield r
    await r.aclose()


async def _spend(session, eur: float) -> None:
    await session.execute(text("DELETE FROM llm_usage"))
    await session.execute(text("INSERT INTO llm_usage (model, input_tokens, output_tokens, cost_eur) VALUES ('x', 1, 1, :c)"), {"c": eur})
    await session.commit()


async def test_budget_warning_is_delivered_once_to_every_channel(session, clean_alerts, mock_http):
    s = get_settings()
    await _spend(session, s.llm_monthly_budget_eur * 0.85)
    assert await check_and_alert(session, clean_alerts, s) == pytest.approx(0.85)
    await check_and_alert(session, clean_alerts, s)  # second check must not spam

    sent = (await mock_http.get("/hooks/_debug")).json()
    assert sorted(a["channel"] for a in sent) == ["telegram", "webhook"]
    assert all("WARNING" in (a.get("text") or "") and "85%" in a["text"] for a in sent)
    assert next(a for a in sent if a["channel"] == "telegram")["chat_id"] == "42"


async def test_budget_exceeded_alert_is_separate_from_warning(session, clean_alerts, mock_http):
    s = get_settings()
    await _spend(session, s.llm_monthly_budget_eur * 0.9)
    await check_and_alert(session, clean_alerts, s)
    await _spend(session, s.llm_monthly_budget_eur * 1.2)
    await check_and_alert(session, clean_alerts, s)
    texts = [a["text"] for a in (await mock_http.get("/hooks/_debug")).json() if a["channel"] == "webhook"]
    assert len(texts) == 2 and "WARNING" in texts[0] and "EXCEEDED" in texts[1] and "paused" in texts[1]


async def test_no_alert_below_threshold(session, clean_alerts, mock_http):
    s = get_settings()
    await _spend(session, s.llm_monthly_budget_eur * 0.5)
    await check_and_alert(session, clean_alerts, s)
    assert (await mock_http.get("/hooks/_debug")).json() == []


async def test_a_dead_channel_does_not_break_the_others_or_leak_its_url(caplog):
    s = get_settings().model_copy(update={"alert_webhook_url": "http://127.0.0.1:9/secret-hook-token"})
    async with httpx.AsyncClient(timeout=2) as client:
        delivered = await send_alert(s, "t", "x", client=client)
    assert delivered == ["telegram"]
    assert "secret-hook-token" not in caplog.text and "123:test-token" not in caplog.text


async def test_alert_without_channels_is_a_noop():
    s = get_settings().model_copy(update={"alert_webhook_url": None, "alert_telegram_bot_token": None})
    assert await send_alert(s, "t", "x") == []


def test_mask_deep_covers_nested_structures():
    data = {"msg": "mail anna@example.de", "list": ["call +49 170 1234567", {"k": ("x@y.io", 5)}], "n": 7}
    out = mask_deep(data)
    flat = str(out)
    assert "anna@" not in flat and "1234567" not in flat and "x@y.io" not in flat
    assert out["n"] == 7 and out["list"][1]["k"][1] == 5


def test_sentry_events_are_scrubbed_before_sending():
    event = {"message": "failed for buyer@example.org", "breadcrumbs": {"values": [{"message": "GET /?email=a.b@c.de"}]}}
    scrubbed = _scrub_event(event, {})
    assert "buyer@" not in str(scrubbed) and "a.b@" not in str(scrubbed)


def test_mock_base_is_the_docker_service():
    assert MOCK_BASE.startswith("http://mock-services")


def test_the_fake_model_costs_nothing_and_a_real_one_does():
    from app.agent.usage import UsageRecord, cost_eur

    s = get_settings()
    assert cost_eur(UsageRecord("fake-rule-based", 10_000, 10_000), s) == 0
    assert cost_eur(UsageRecord("claude-haiku-4-5-20251001", 1_000_000, 1_000_000), s) > 0
