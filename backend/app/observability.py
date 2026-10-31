"""Sentry, Prometheus metrics, Langfuse tracing (each is optional and driven by env)."""

import logging
import os

from prometheus_client import Counter, Gauge, Histogram

from app.config import Settings

log = logging.getLogger(__name__)

CHAT_TURNS = Counter("chat_turns_total", "Chat turns handled", ["lang", "outcome"])
ESCALATIONS = Counter("chat_escalations_total", "Handovers to a human", ["reason"])
NO_ANSWER = Counter("chat_no_answer_total", "Questions without a knowledge-base answer")
LLM_COST_EUR = Counter("llm_cost_eur_total", "LLM spend in EUR")
LLM_TOKENS = Counter("llm_tokens_total", "LLM tokens", ["direction"])
TTFT = Histogram("chat_first_token_seconds", "Time to first streamed token",
                 buckets=(0.25, 0.5, 1, 1.5, 2, 2.5, 3, 5, 10))
TURN_SECONDS = Histogram("chat_turn_seconds", "Total turn duration", buckets=(0.5, 1, 2, 4, 8, 15, 30))
BUDGET_RATIO = Gauge("llm_budget_used_ratio", "Share of the monthly LLM budget already used")
BLOCKED = Counter("chat_blocked_total", "Requests blocked by guards", ["kind"])


def init_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn, environment=settings.app_env, traces_sample_rate=0.05,
        send_default_pii=False,  # GDPR: no IPs/cookies/user data
        before_send=_scrub_event,
        before_breadcrumb=_scrub_event,
    )


def _scrub_event(event, hint):  # noqa: ARG001
    """Last line of defence: emails/phones inside messages, breadcrumbs or locals never leave the server."""
    from app.logging_conf import mask_deep

    return mask_deep(event)


def init_langfuse_env(settings: Settings) -> bool:
    """Langfuse's LangChain handler reads its config from env vars."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return False
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
    os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key.get_secret_value()
    os.environ["LANGFUSE_HOST"] = settings.langfuse_host
    try:
        # The client is a process-wide singleton the LangChain handler picks up; `mask` runs on every
        # input/output/metadata before it is sent, so customer emails/phones never reach Langfuse.
        from langfuse import Langfuse

        Langfuse(public_key=settings.langfuse_public_key, secret_key=settings.langfuse_secret_key.get_secret_value(),
                 host=settings.langfuse_host, mask=_langfuse_mask)
    except Exception:
        log.exception("Langfuse init failed; tracing disabled")
        return False
    return True


def _langfuse_mask(data=None, **_kwargs):
    from app.logging_conf import mask_deep

    return mask_deep(data)


def langfuse_callbacks(enabled: bool) -> list:
    if not enabled:
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception:
        log.warning("Langfuse handler unavailable; tracing disabled")
        return []
