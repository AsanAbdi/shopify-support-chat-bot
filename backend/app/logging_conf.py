"""JSON logging with PII masking (GDPR: no emails or phone numbers in logs)."""

import json
import logging
import re
import sys
from datetime import UTC, datetime

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# International and local phone formats: at least 8 digits, optional separators
PHONE_RE = re.compile(r"(?<![\w/])\+?\d[\d\s().\-/]{6,}\d(?![\w/])")


def mask_pii(text: str) -> str:
    def _email(m: re.Match) -> str:
        local, _, domain = m.group(0).partition("@")
        return f"{local[:1]}***@{domain}"

    def _phone(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return m.group(0) if len(digits) < 8 else "[phone]"

    return PHONE_RE.sub(_phone, EMAIL_RE.sub(_email, text))


class PiiMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_pii(record.getMessage())
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("conversation_id", "request_id", "event"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = mask_pii(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(PiiMaskingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def mask_deep(obj):
    """Recursively mask emails/phones in any JSON-like structure (used for Sentry events and Langfuse traces)."""
    if isinstance(obj, str):
        return mask_pii(obj)
    if isinstance(obj, dict):
        return {k: mask_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_deep(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(mask_deep(v) for v in obj)
    return obj
