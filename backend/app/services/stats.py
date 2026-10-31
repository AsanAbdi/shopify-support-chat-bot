from datetime import timedelta
from decimal import Decimal

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Conversation, LlmUsage, Message, UnansweredQuestion, _now
from app.services.budget import month_spend_eur


async def build_stats(session: AsyncSession, settings: Settings, days: int = 30) -> dict:
    since = _now() - timedelta(days=days)
    day = cast(Conversation.created_at, Date)
    per_day = (await session.execute(
        select(day.label("d"), func.count().label("n")).where(Conversation.created_at >= since).group_by(day).order_by(day)
    )).all()

    total, escalated = (await session.execute(
        select(func.count(), func.coalesce(func.sum(case((Conversation.escalated, 1), else_=0)), 0))
        .where(Conversation.created_at >= since)
    )).one()
    closed_without_operator = round(100 * (total - escalated) / total, 1) if total else None

    top_unanswered = (await session.execute(
        select(func.lower(UnansweredQuestion.question).label("q"), func.count().label("n"))
        .where(UnansweredQuestion.created_at >= since).group_by("q").order_by(func.count().desc()).limit(10)
    )).all()

    cost_per_day = (await session.execute(
        select(cast(LlmUsage.created_at, Date).label("d"), func.sum(LlmUsage.cost_eur))
        .where(LlmUsage.created_at >= since).group_by("d").order_by("d")
    )).all()
    cost_period = sum((Decimal(c) for _, c in cost_per_day), Decimal(0))
    month_cost = await month_spend_eur(session)

    up, down = (await session.execute(
        select(
            func.coalesce(func.sum(case((Message.feedback == 1, 1), else_=0)), 0),
            func.coalesce(func.sum(case((Message.feedback == -1, 1), else_=0)), 0),
        ).where(Message.created_at >= since)
    )).one()

    return {
        "period_days": days,
        "conversations_total": total,
        "conversations_per_day": [{"date": d.isoformat(), "count": n} for d, n in per_day],
        "escalated": escalated,
        "closed_without_operator_pct": closed_without_operator,
        "top_unanswered": [{"question": q, "count": n} for q, n in top_unanswered],
        "llm_cost_eur_period": float(round(cost_period, 4)),
        "llm_cost_eur_month": float(round(month_cost, 4)),
        "llm_budget_eur": settings.llm_monthly_budget_eur,
        "llm_cost_per_conversation_eur": float(round(cost_period / total, 5)) if total else None,
        "llm_cost_per_day": [{"date": d.isoformat(), "eur": float(round(c, 4))} for d, c in cost_per_day],
        "feedback_up": up,
        "feedback_down": down,
    }
