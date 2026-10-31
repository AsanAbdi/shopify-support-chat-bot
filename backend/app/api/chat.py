import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Document
from app.observability import BLOCKED
from app.ratelimit import allow, http_limit
from app.security import client_ip, ip_hash
from app.services.chat import ChatService

log = logging.getLogger(__name__)
router = APIRouter()


class ChatIn(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    text: str = Field(max_length=4000)


class FeedbackIn(BaseModel):
    session_id: str = Field(max_length=64)
    message_id: int
    value: int


def _origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    return "*" in allowed or (origin is not None and origin in allowed)


@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    c = ws.app.state.container
    if not _origin_allowed(ws.headers.get("origin"), c.settings.cors_origins):
        BLOCKED.labels("origin").inc()
        await ws.close(code=4403)
        return
    await ws.accept()
    service = ChatService(c)
    ip = ws.client.host if ws.client else "unknown"
    session_id: str | None = ws.query_params.get("session_id")
    try:
        while True:
            try:
                data = json.loads(await ws.receive_text())
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "code": "bad_json", "text": "Invalid message"})
                continue
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
                continue
            if data.get("type") != "message":
                continue
            if not await allow(c.redis, "chat", ip, c.settings.rate_limit_chat_per_min):
                BLOCKED.labels("rate_chat").inc()
                await ws.send_json({"type": "error", "code": "rate_limited", "text": "Too many messages, please slow down."})
                continue
            session_id = data.get("session_id") or session_id
            async for event in service.stream_turn(session_id, str(data.get("text", "")), ip_hash(ip)):
                if event["type"] == "session":
                    session_id = event["id"]
                await ws.send_json(event)
    except WebSocketDisconnect:
        return


@router.post("/api/chat/stream", dependencies=[Depends(http_limit("chat_http", "rate_limit_chat_per_min"))])
async def sse_chat(body: ChatIn, request: Request) -> StreamingResponse:
    """Server-Sent Events fallback for environments where WebSockets are blocked."""
    service = ChatService(request.app.state.container)

    async def gen():
        async for event in service.stream_turn(body.session_id, body.text, ip_hash(client_ip(request))):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/chat/history", dependencies=[Depends(http_limit("history", "rate_limit_http_per_min"))])
async def history(request: Request, session_id: Annotated[str, Query(max_length=64)]) -> dict:
    data = await ChatService(request.app.state.container).history(session_id)
    if data is None:
        raise HTTPException(404, "Session expired or unknown")
    return data


@router.post("/api/chat/feedback", dependencies=[Depends(http_limit("feedback", "rate_limit_http_per_min"))])
async def feedback(body: FeedbackIn, request: Request) -> dict:
    ok = await ChatService(request.app.state.container).set_feedback(body.session_id, body.message_id, body.value)
    if not ok:
        raise HTTPException(400, "Invalid feedback")
    return {"ok": True}


@router.get("/api/kb/{doc_id}/file")
async def kb_file(doc_id: int, request: Request, session: Annotated[AsyncSession, Depends(get_session)]) -> FileResponse:
    """Public download of a knowledge-base file that was cited as a source."""
    doc = await session.get(Document, doc_id)
    if doc is None or doc.source_type != "file" or not doc.stored_path or not Path(doc.stored_path).is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(doc.stored_path, filename=doc.source_ref, content_disposition_type="inline")
