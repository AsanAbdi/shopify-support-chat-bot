"""Customer-facing wording for Shopify status codes.

The model never sees raw codes such as OUT_FOR_DELIVERY or FULFILLED: a model repeats whatever it is shown, and a prompt
rule like "explain it in plain words" is only a request. Translating here makes leaking a code impossible. English is
what the model gets (it translates into the customer's language); German and Dutch exist for the offline fake model.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import Settings

_EN: dict[str, dict[str, str]] = {
    "fulfillment": {
        "FULFILLED": "shipped",
        "PARTIALLY_FULFILLED": "partly shipped (the rest follows in another parcel)",
        "UNFULFILLED": "being prepared, not shipped yet",
        "OPEN": "being prepared, not shipped yet",
        "IN_PROGRESS": "being prepared, not shipped yet",
        "PENDING_FULFILLMENT": "waiting to be prepared",
        "SCHEDULED": "scheduled for shipping",
        "ON_HOLD": "on hold",
        "REQUEST_DECLINED": "could not be prepared",
        "RESTOCKED": "cancelled, the items went back into stock",
        "CANCELLED": "cancelled",
    },
    "financial": {
        "PAID": "paid",
        "PENDING": "payment still pending",
        "AUTHORIZED": "payment authorised, it is charged when the order ships",
        "PARTIALLY_PAID": "partly paid",
        "PARTIALLY_REFUNDED": "partly refunded",
        "REFUNDED": "refunded",
        "VOIDED": "payment cancelled",
        "EXPIRED": "payment expired",
    },
    "delivery": {
        "IN_TRANSIT": "on its way",
        "OUT_FOR_DELIVERY": "out for delivery today",
        "DELIVERED": "delivered",
        "ATTEMPTED_DELIVERY": "the carrier could not deliver it and will try again",
        "DELAYED": "delayed",
        "READY_FOR_PICKUP": "waiting for the customer at a pickup point",
        "PICKED_UP": "picked up",
        "CONFIRMED": "accepted by the carrier",
        "LABEL_PRINTED": "label created, the carrier will collect it soon",
        "LABEL_PURCHASED": "label created, the carrier will collect it soon",
        "LABEL_VOIDED": "being re-labelled",
        "SUBMITTED": "handed to the carrier",
        "FULFILLED": "shipped",
        "MARKED_AS_FULFILLED": "shipped",
        "NOT_DELIVERED": "could not be delivered",
        "FAILURE": "delivery failed",
        "CANCELED": "shipment cancelled",
    },
    "return": {
        "RETURN_REQUESTED": "return requested",
        "IN_PROGRESS": "return on its way back to the shop",
        "INSPECTION_COMPLETE": "return received and checked",
        "RETURNED": "returned",
        "RETURN_FAILED": "return did not arrive",
    },
    "cancel": {
        "CUSTOMER": "cancelled at the customer's request",
        "DECLINED": "payment was declined",
        "INVENTORY": "an item was out of stock",
        "STAFF": "cancelled by the shop",
        "FRAUD": "cancelled by the shop",
        "OTHER": "cancelled",
    },
}

_DE: dict[str, dict[str, str]] = {
    "fulfillment": {
        "FULFILLED": "versendet", "PARTIALLY_FULFILLED": "teilweise versendet", "UNFULFILLED": "in Vorbereitung, noch nicht versendet",
        "OPEN": "in Vorbereitung, noch nicht versendet", "IN_PROGRESS": "in Vorbereitung, noch nicht versendet",
        "ON_HOLD": "angehalten", "CANCELLED": "storniert",
    },
    "delivery": {
        "IN_TRANSIT": "unterwegs", "OUT_FOR_DELIVERY": "heute in Zustellung", "DELIVERED": "zugestellt", "DELAYED": "verspätet",
        "ATTEMPTED_DELIVERY": "Zustellung gescheitert, ein neuer Versuch folgt", "READY_FOR_PICKUP": "zur Abholung bereit",
    },
}

_NL: dict[str, dict[str, str]] = {
    "fulfillment": {
        "FULFILLED": "verzonden", "PARTIALLY_FULFILLED": "gedeeltelijk verzonden", "UNFULFILLED": "in voorbereiding, nog niet verzonden",
        "OPEN": "in voorbereiding, nog niet verzonden", "IN_PROGRESS": "in voorbereiding, nog niet verzonden",
        "ON_HOLD": "in de wacht gezet", "CANCELLED": "geannuleerd",
    },
    "delivery": {
        "IN_TRANSIT": "onderweg", "OUT_FOR_DELIVERY": "vandaag in bezorging", "DELIVERED": "bezorgd", "DELAYED": "vertraagd",
        "ATTEMPTED_DELIVERY": "bezorging mislukt, er volgt een nieuwe poging", "READY_FOR_PICKUP": "klaar om op te halen",
    },
}

_TABLES = {"en": _EN, "de": _DE, "nl": _NL}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def label(kind: str, code: str | None, lang: str = "en") -> str | None:
    """Plain wording for a status code; None for 'nothing to say' (NO_RETURN, empty). Unknown codes are lower-cased and
    de-underscored, so even a status Shopify adds tomorrow can never reach a customer as SHOUTING_SNAKE_CASE."""
    if not code or code == "NO_RETURN":
        return None
    table = _TABLES.get(lang, _EN)
    return table.get(kind, {}).get(code) or _EN.get(kind, {}).get(code) or code.replace("_", " ").lower()


_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def shop_clock(settings: Settings) -> tuple[ZoneInfo, datetime]:
    """'Now' in the shop's time zone. A model has no clock: it must be told, and told the right zone (a parcel that is
    due 'tomorrow' for a customer in Amsterdam is not 'tomorrow' on a server running UTC)."""
    tz = ZoneInfo(settings.shop_timezone)
    return tz, datetime.now(tz)


def describe_now(settings: Settings) -> str:
    tz, now = shop_clock(settings)
    return f"{_WEEKDAYS[now.weekday()]} {now.day} {_MONTHS[now.month - 1]} {now.year}, {now:%H:%M} ({settings.shop_timezone})"


def human_date(d: date | datetime | None, today: date | None = None, tz: ZoneInfo | None = None) -> str | None:
    """'tomorrow, Thursday 22 October 2026'. Relative words are computed here, never left to the model: asked for 'today',
    an LLM that was not told the date guesses, and guesses wrong by a day (a customer-visible bug that actually happened)."""
    if d is None:
        return None
    if isinstance(d, datetime):
        d = (d.astimezone(tz) if tz and d.tzinfo else d).date()
    text = f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]} {d.year}"
    if today is None:
        return text
    delta = (d - today).days
    relative = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(delta) or (f"in {delta} days" if delta > 1 else f"{-delta} days ago")
    return f"{relative}, {text}"


def is_past(d: date | datetime | None, today: date, tz: ZoneInfo | None = None) -> bool:
    if d is None:
        return False
    if isinstance(d, datetime):
        d = (d.astimezone(tz) if tz and d.tzinfo else d).date()
    return d < today
