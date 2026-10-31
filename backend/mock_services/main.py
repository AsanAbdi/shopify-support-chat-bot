"""Mock services for local/staging use (NOT for production): Shopify Admin GraphQL, Freshdesk v2, alert receivers,
a tiny help-center site. Shopify lives in shopify_api.py; this module wires everything together."""

import itertools
import re
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from mock_services import shopify_api

app = FastAPI(title="mock-services")
app.include_router(shopify_api.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# ---------------- Freshdesk v2 ----------------
TICKETS: dict[int, dict] = {}
_ticket_ids = itertools.count(5001)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FD_FAIL = {"next": 0, "status": 503, "retry_after": None}


def _fd_error(field: str, message: str, code: str = "invalid_value") -> JSONResponse:
    return JSONResponse({"description": "Validation failed", "errors": [{"field": field, "message": message, "code": code}]}, status_code=400)


@app.post("/freshdesk/api/v2/tickets")
async def create_ticket(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return JSONResponse({"code": "invalid_credentials", "message": "You have to be logged in to perform this action."}, status_code=401)
    if FD_FAIL["next"] > 0:
        FD_FAIL["next"] -= 1
        headers = {"Retry-After": str(FD_FAIL["retry_after"])} if FD_FAIL["retry_after"] else {}
        return JSONResponse({"message": "Too many requests" if FD_FAIL["status"] == 429 else "Service unavailable"}, status_code=FD_FAIL["status"], headers=headers)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"code": "invalid_json", "message": "Request body has invalid json format"}, status_code=400)

    for required in ("subject", "description"):
        if not isinstance(body.get(required), str) or not body[required].strip():
            return _fd_error(required, "It should be a/an String" if required in body else "Mandatory attribute missing", "missing_field")
    if not any(body.get(k) for k in ("email", "requester_id", "phone", "twitter_id", "facebook_id", "unique_external_id")):
        return _fd_error("email", "Please specify at least one of email, requester_id, phone, twitter_id, facebook_id, unique_external_id", "missing_field")
    if body.get("email") and not _EMAIL.match(str(body["email"])):
        return _fd_error("email", "It should be in the 'valid email address' format")
    if len(body["subject"]) > 255:
        return _fd_error("subject", "Has 400 characters, it can have maximum of 255 characters")
    if body.get("priority", 1) not in (1, 2, 3, 4):
        return _fd_error("priority", "It should be one of these values: '1,2,3,4'")
    if body.get("status", 2) not in (2, 3, 4, 5):
        return _fd_error("status", "It should be one of these values: '2,3,4,5'")
    if not isinstance(body.get("tags", []), list) or any(not isinstance(t, str) or len(t) > 32 for t in body.get("tags", [])):
        return _fd_error("tags", "It should contain elements of type String with at most 32 characters")

    tid = next(_ticket_ids)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    requester_id = 90_000_000_000 + abs(hash(body.get("email", ""))) % 10_000_000
    TICKETS[tid] = {
        "id": tid, "subject": body["subject"], "description": body["description"], "description_text": re.sub(r"<[^>]+>", " ", body["description"]).strip(),
        "email": body.get("email"), "requester_id": requester_id, "status": body.get("status", 2), "priority": body.get("priority", 1),
        "source": body.get("source", 2), "tags": body.get("tags", []), "type": None, "spam": False, "is_escalated": False, "due_by": now,
        "created_at": now, "updated_at": now, "responder_id": None, "group_id": None, "company_id": None, "custom_fields": body.get("custom_fields", {}),
    }
    return JSONResponse(TICKETS[tid], status_code=201, headers={"X-RateLimit-Total": "100", "X-RateLimit-Remaining": "99", "X-RateLimit-Used-CurrentRequest": "1"})


@app.get("/freshdesk/api/v2/tickets/{ticket_id}")
async def get_ticket(ticket_id: int) -> dict:
    if ticket_id not in TICKETS:
        raise HTTPException(404, "not found")
    return TICKETS[ticket_id]


@app.get("/freshdesk/_debug/tickets")
async def list_tickets() -> list[dict]:
    return list(TICKETS.values())


@app.post("/freshdesk/_debug/fail")
async def freshdesk_fail(request: Request) -> dict:
    cfg = await request.json()
    FD_FAIL.update(next=cfg.get("next", 0), status=cfg.get("status", 503), retry_after=cfg.get("retry_after"))
    return FD_FAIL


# ---------------- alert receivers (Slack-style webhook + Telegram Bot API) ----------------
ALERTS: list[dict] = []


@app.post("/hooks/alert")
async def alert_hook(request: Request) -> dict:
    ALERTS.append({"channel": "webhook", **await request.json()})
    return {"ok": True}


@app.post("/telegram/bot{token}/sendMessage")
async def telegram_send(token: str, request: Request) -> dict:  # noqa: ARG001
    ALERTS.append({"channel": "telegram", **await request.json()})
    return {"ok": True, "result": {"message_id": len(ALERTS)}}


@app.get("/hooks/_debug")
async def alerts_debug() -> list[dict]:
    return ALERTS


@app.delete("/hooks/_debug")
async def alerts_clear() -> dict:
    ALERTS.clear()
    return {"ok": True}


# ---------------- tiny "help center" for URL-indexing tests/demos ----------------
PAGES: dict[str, str] = {
    "gift-wrap.html": (
        "<html><head><title>Gift wrapping</title></head><body><nav>Home | Shop</nav><article>"
        "<h1>Gift wrapping</h1><h2>Service</h2><p>We offer free gift wrapping for all orders in December.</p>"
        "<h2>Cards</h2><p>A handwritten card costs EUR 2.</p></article><footer>Imprint</footer></body></html>"
    ),
}


@app.get("/pages/{name}")
async def get_page(name: str) -> HTMLResponse:
    if name not in PAGES:
        raise HTTPException(404, "not found")
    return HTMLResponse(PAGES[name])


@app.put("/pages/{name}")
async def put_page(name: str, request: Request) -> dict:
    PAGES[name] = (await request.body()).decode("utf-8")
    return {"ok": True}


def _pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return bytes(out)


@app.get("/files/terms.pdf")
async def terms_pdf() -> Response:
    return Response(_pdf("Terms: returns are accepted within 30 days of delivery."), media_type="application/pdf")
