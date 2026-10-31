"""Operational alerts (budget, ...). Channels are optional and independent; a failing channel never breaks a chat turn."""

import logging

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


async def send_alert(settings: Settings, title: str, text: str, *, client: httpx.AsyncClient | None = None) -> list[str]:
    """Returns the channels that accepted the alert. Sentry gets it separately via logger.error."""
    delivered: list[str] = []
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.integration_timeout_s)
    try:
        # Works with Slack/Mattermost/Teams-style incoming webhooks ("text") and generic receivers ("title", "text")
        if settings.alert_webhook_url and await _post(
            client, settings.alert_webhook_url, {"title": title, "text": f"*{title}*\n{text}"}
        ):
            delivered.append("webhook")
        if settings.alert_telegram_bot_token and settings.alert_telegram_chat_id:
            url = f"{settings.alert_telegram_api_url}/bot{settings.alert_telegram_bot_token.get_secret_value()}/sendMessage"
            if await _post(client, url, {"chat_id": settings.alert_telegram_chat_id, "text": f"{title}\n{text}"}):
                delivered.append("telegram")
    finally:
        if own:
            await client.aclose()
    return delivered


async def _post(client: httpx.AsyncClient, url: str, payload: dict) -> bool:
    try:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        # never log the URL: it contains the webhook secret / bot token
        log.error("alert delivery failed: %s", type(exc).__name__)
        return False
