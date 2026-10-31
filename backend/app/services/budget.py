"""Monthly LLM spend guard: alert at a threshold, hard stop at 100%."""

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import LlmUsage
from app.observability import BUDGET_RATIO
from app.services.alerts import send_alert

log = logging.getLogger(__name__)


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now().astimezone()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_spend_eur(session: AsyncSession) -> Decimal:
    q = select(func.coalesce(func.sum(LlmUsage.cost_eur), 0)).where(LlmUsage.created_at >= month_start())
    return Decimal((await session.execute(q)).scalar_one())


async def budget_ratio(session: AsyncSession, settings: Settings) -> float:
    ratio = float(await month_spend_eur(session)) / max(settings.llm_monthly_budget_eur, 0.01)
    BUDGET_RATIO.set(ratio)
    return ratio


async def check_and_alert(session: AsyncSession, redis, settings: Settings) -> float:
    ratio = await budget_ratio(session, settings)
    key = f"budget-alert:{month_start():%Y-%m}"
    for level, label in ((1.0, "EXCEEDED"), (settings.llm_budget_alert_ratio, "WARNING")):
        if ratio >= level:
            if await redis.set(f"{key}:{label}", "1", nx=True, ex=40 * 86400):
                # logger.error is forwarded to Sentry by its logging integration -> alert
                log.error("LLM budget %s: %.0f%% of EUR %.2f used", label, ratio * 100, settings.llm_monthly_budget_eur,
                          extra={"event": "budget_alert"})
                await send_alert(
                    settings, f"LLM budget {label}",
                    f"{ratio * 100:.0f}% of the monthly budget (EUR {settings.llm_monthly_budget_eur:.2f}) is used."
                    + (" The assistant is paused until next month or until the budget is raised." if label == "EXCEEDED" else ""),
                )
            break
    return ratio
