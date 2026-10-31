"""Freshdesk API v2 client (create ticket)."""

import asyncio
import html

import httpx

from app.config import Settings, get_settings
from app.integrations.errors import IntegrationError

PRIORITY_MEDIUM, STATUS_OPEN, SOURCE_CHAT = 2, 2, 7


class FreshdeskClient:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.s = settings or get_settings()
        self._client = client or httpx.AsyncClient(timeout=self.s.integration_timeout_s)

    async def create_ticket(
        self, *, email: str, subject: str, summary: str, history: list[tuple[str, str]], lang: str,
        order_number: str | None, reason: str, urgent: bool = False,
    ) -> str:
        transcript = "".join(
            f"<p><b>{'Customer' if role == 'user' else 'Bot'}:</b> {html.escape(text)}</p>" for role, text in history
        )
        description = (
            f"<h3>Summary</h3><p>{html.escape(summary).replace(chr(10), '<br>')}</p>"
            f"<p><b>Language:</b> {lang.upper()} &middot; <b>Order:</b> {html.escape(order_number or '-')} "
            f"&middot; <b>Reason:</b> {html.escape(reason)}</p><h3>Conversation</h3>{transcript}"
        )
        payload = {
            "email": email, "subject": subject[:250], "description": description,
            "priority": 3 if urgent else PRIORITY_MEDIUM, "status": STATUS_OPEN, "source": SOURCE_CHAT,
            "tags": ["ai-assistant", f"lang-{lang}", f"reason-{reason}"],
        }
        auth = (self.s.freshdesk_api_key.get_secret_value(), "X")
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(f"{self.s.freshdesk_base_url}/api/v2/tickets", json=payload, auth=auth)
                if resp.status_code in (429, 502, 503, 504):
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return str(resp.json()["id"])
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        raise IntegrationError(f"Freshdesk unavailable: {last}")

    async def aclose(self) -> None:
        await self._client.aclose()
