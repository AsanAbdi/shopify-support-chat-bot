import re
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Chunk, Conversation, Document, Message, User
from app.rag.loaders import LOADERS
from app.ratelimit import http_limit
from app.security import (
    check_credentials,
    create_token,
    current_user,
    hash_password,
    require_admin,
)
from app.services.gdpr import delete_conversations, delete_customer_data
from app.services.stats import build_stats
from app.workers.tasks import index_document_task, reindex_all_task, sync_catalog_task

router = APIRouter(prefix="/api")
Session = Annotated[AsyncSession, Depends(get_session)]
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


# ---------------------------- auth ----------------------------
class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@router.post("/auth/login", dependencies=[Depends(http_limit("login", "rate_limit_login_per_min"))])
async def login(body: LoginIn, session: Session) -> dict:
    user = (await session.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if not check_credentials(user, body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    assert user is not None
    return {"access_token": create_token(user), "token_type": "bearer", "role": user.role, "username": user.username}


@router.get("/admin/me")
async def me(user: Annotated[User, Depends(current_user)]) -> dict:
    return {"username": user.username, "role": user.role}


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    password: str = Field(min_length=10, max_length=128)
    role: Literal["admin", "agent"] = "agent"


@router.post("/admin/users", status_code=201, dependencies=[Depends(require_admin)])
async def create_user(body: UserIn, session: Session) -> dict:
    session.add(User(username=body.username, password_hash=hash_password(body.password), role=body.role))
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "Username already exists") from None
    return {"username": body.username, "role": body.role}


@router.get("/admin/users", dependencies=[Depends(require_admin)])
async def list_users(session: Session) -> list[dict]:
    rows = (await session.execute(select(User).order_by(User.id))).scalars()
    return [{"id": u.id, "username": u.username, "role": u.role, "is_active": u.is_active} for u in rows]


class PasswordChangeIn(BaseModel):
    old_password: str = Field(max_length=256)
    new_password: str = Field(min_length=10, max_length=128)


@router.post("/auth/change-password", dependencies=[Depends(http_limit("login", "rate_limit_login_per_min"))])
async def change_password(body: PasswordChangeIn, user: Annotated[User, Depends(current_user)], session: Session) -> dict:
    if not check_credentials(user, body.old_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is wrong")
    user.password_hash = hash_password(body.new_password)
    await session.commit()
    return {"ok": True}


class UserPatch(BaseModel):
    role: Literal["admin", "agent"] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=128)  # admin reset


async def _active_admins(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(User).where(User.role == "admin", User.is_active))).scalar_one()


@router.patch("/admin/users/{user_id}")
async def patch_user(user_id: int, body: UserPatch, session: Session, me_: Annotated[User, Depends(require_admin)]) -> dict:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Not found")
    losing_admin = user.role == "admin" and user.is_active and (body.role == "agent" or body.is_active is False)
    if losing_admin and (user.id == me_.id or await _active_admins(session) <= 1):
        raise HTTPException(409, "Cannot demote or deactivate yourself or the last active admin")
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    await session.commit()
    return {"id": user.id, "username": user.username, "role": user.role, "is_active": user.is_active}


@router.delete("/admin/users/{user_id}", status_code=204)
async def delete_user(user_id: int, session: Session, me_: Annotated[User, Depends(require_admin)]) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Not found")
    if user.id == me_.id or (user.role == "admin" and user.is_active and await _active_admins(session) <= 1):
        raise HTTPException(409, "Cannot delete yourself or the last active admin")
    await session.delete(user)
    await session.commit()


# ---------------------------- documents ----------------------------
def _doc_out(d: Document) -> dict:
    return {
        "id": d.id, "title": d.title, "source_type": d.source_type, "source": d.source_ref, "status": d.status,
        "error": d.error, "chunks": d.chunks_count, "updated_at": d.updated_at, "indexed_at": d.indexed_at,
    }


@router.get("/admin/documents", dependencies=[Depends(current_user)])
async def list_documents(session: Session) -> list[dict]:
    return [_doc_out(d) for d in (await session.execute(select(Document).order_by(Document.id.desc()))).scalars()]


@router.post("/admin/documents", status_code=202, dependencies=[Depends(require_admin)])
async def upload_document(session: Session, file: Annotated[UploadFile, File()]) -> dict:
    name = Path(file.filename or "").name
    if Path(name).suffix.lower() not in LOADERS:
        raise HTTPException(415, f"Supported: {', '.join(sorted(LOADERS))}")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (max 25 MB)")
    updir = Path(get_settings().upload_dir)
    updir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._\-]", "_", name)
    path = updir / f"{uuid.uuid4().hex}_{safe}"
    path.write_bytes(data)

    doc = (await session.execute(select(Document).where(Document.source_ref == name))).scalar_one_or_none()
    if doc is None:
        doc = Document(title=Path(name).stem, source_type="file", source_ref=name, stored_path=str(path), status="pending")
        session.add(doc)
    else:  # same filename = new version of the document; only changed chunks get re-embedded
        old = doc.stored_path
        doc.stored_path, doc.status = str(path), "pending"
        if old:
            Path(old).unlink(missing_ok=True)
    await session.commit()
    await session.refresh(doc)  # server-side onupdate/defaults are expired after commit
    index_document_task.delay(doc.id)
    return _doc_out(doc)


class UrlIn(BaseModel):
    url: HttpUrl


@router.post("/admin/documents/url", status_code=202, dependencies=[Depends(require_admin)])
async def add_url(body: UrlIn, session: Session) -> dict:
    url = str(body.url)
    doc = (await session.execute(select(Document).where(Document.source_ref == url))).scalar_one_or_none()
    if doc is None:
        doc = Document(title=url, source_type="url", source_ref=url, status="pending")
        session.add(doc)
    doc.status = "pending"
    await session.commit()
    await session.refresh(doc)  # server-side onupdate/defaults are expired after commit
    index_document_task.delay(doc.id)
    return _doc_out(doc)


@router.post("/admin/documents/{doc_id}/reindex", status_code=202, dependencies=[Depends(require_admin)])
async def reindex(doc_id: int, session: Session, force: bool = False) -> dict:
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "Not found")
    doc.status = "pending"
    await session.commit()
    await session.refresh(doc)
    index_document_task.delay(doc_id, force)
    return _doc_out(doc)


@router.delete("/admin/documents/{doc_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_document(doc_id: int, session: Session) -> None:
    doc = await session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "Not found")
    if doc.stored_path:
        Path(doc.stored_path).unlink(missing_ok=True)
    await session.delete(doc)  # chunks are removed by ON DELETE CASCADE
    await session.commit()


@router.get("/admin/documents/{doc_id}/chunks", dependencies=[Depends(current_user)])
async def document_chunks(doc_id: int, session: Session) -> list[dict]:
    rows = (await session.execute(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.idx))).scalars()
    return [{"idx": c.idx, "content": c.content} for c in rows]


# ---------------------------- conversations ----------------------------
@router.get("/admin/conversations", dependencies=[Depends(current_user)])
async def list_conversations(
    session: Session,
    flt: Annotated[Literal["all", "escalated", "downvoted", "no_answer"], Query(alias="filter")] = "all",
    q: Annotated[str | None, Query(max_length=100, description="email, order number, ticket, id prefix or text of a message")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    search = []
    if q and (term := q.strip().lstrip("#")):
        in_text = exists().where(Message.conversation_id == Conversation.id, Message.content.icontains(term, autoescape=True))
        search.append(or_(
            Conversation.customer_email.icontains(term, autoescape=True), Conversation.order_number.icontains(term, autoescape=True),
            Conversation.id.istartswith(term, autoescape=True), Conversation.ticket_id == term, in_text,
        ))
    by_filter = {"escalated": Conversation.escalated, "downvoted": Conversation.has_downvote, "no_answer": Conversation.had_no_answer}
    conds = [*search, *([by_filter[flt]] if flt in by_filter else [])]

    # counters for the filter pills follow the search box, not the selected pill
    counts = (await session.execute(
        select(func.count(), func.count().filter(Conversation.escalated), func.count().filter(Conversation.has_downvote),
               func.count().filter(Conversation.had_no_answer)).where(*search)
    )).one()
    first_message = (select(Message.content).where(Message.conversation_id == Conversation.id, Message.role == "user")
                     .order_by(Message.id).limit(1).scalar_subquery())
    message_count = select(func.count(Message.id)).where(Message.conversation_id == Conversation.id).scalar_subquery()
    rows = (await session.execute(
        select(Conversation, first_message.label("preview"), message_count.label("n")).where(*conds)
        .order_by(Conversation.last_activity_at.desc()).limit(limit).offset(offset)
    )).all()
    total = counts[{"all": 0, "escalated": 1, "downvoted": 2, "no_answer": 3}[flt]]
    return {
        "total": total,
        "counts": {"all": counts[0], "escalated": counts[1], "downvoted": counts[2], "no_answer": counts[3]},
        "items": [
            {"id": c.id, "created_at": c.created_at, "last_activity_at": c.last_activity_at, "lang": c.lang,
             "email": c.customer_email, "order_number": c.order_number, "escalated": c.escalated,
             "reason": c.escalation_reason, "ticket_id": c.ticket_id, "downvoted": c.has_downvote, "no_answer": c.had_no_answer,
             "preview": (preview or "")[:140], "messages": n}
            for c, preview, n in rows
        ],
    }


@router.get("/admin/conversations/{conv_id}", dependencies=[Depends(current_user)])
async def get_conversation(conv_id: str, session: Session) -> dict:
    c = await session.get(Conversation, conv_id)
    if c is None:
        raise HTTPException(404, "Not found")
    msgs = (await session.execute(select(Message).where(Message.conversation_id == conv_id).order_by(Message.id))).scalars()
    return {
        "id": c.id, "lang": c.lang, "email": c.customer_email, "order_number": c.order_number, "escalated": c.escalated,
        "reason": c.escalation_reason, "ticket_id": c.ticket_id, "ticket_summary": c.ticket_summary,
        "created_at": c.created_at, "last_activity_at": c.last_activity_at, "downvoted": c.has_downvote,
        "messages": [{"role": m.role, "content": m.content, "sources": m.sources, "feedback": m.feedback,
                      "no_answer": m.no_answer, "at": m.created_at} for m in msgs],
    }


@router.delete("/admin/conversations/{conv_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_conversation(conv_id: str, session: Session) -> None:
    """Remove one conversation with its messages and the agent's checkpoint (test chats, or a single erasure request)."""
    if await session.get(Conversation, conv_id) is None:
        raise HTTPException(404, "Not found")
    await delete_conversations(session, [conv_id])
    await session.commit()


# ---------------------------- stats / ops ----------------------------
@router.get("/admin/stats", dependencies=[Depends(current_user)])
async def stats(request: Request, session: Session, days: Annotated[int, Query(ge=1, le=365)] = 30) -> dict:
    return await build_stats(session, request.app.state.container.settings, days)


@router.post("/admin/catalog/sync", status_code=202, dependencies=[Depends(require_admin)])
async def trigger_catalog_sync() -> dict:
    sync_catalog_task.delay()
    return {"queued": True}


@router.post("/admin/reindex-all", status_code=202, dependencies=[Depends(require_admin)])
async def reindex_all(session: Session) -> dict:
    """Re-embed every document and the catalog in the background (after changing the embedding model, or to repair the index).
    Documents are marked pending right away, so the admin page shows the progress instead of looking idle."""
    result = await session.execute(update(Document).values(status="pending", error=None))
    await session.commit()
    reindex_all_task.delay()
    return {"queued": True, "documents": result.rowcount}


@router.delete("/admin/gdpr/customer", dependencies=[Depends(require_admin)])
async def gdpr_delete(session: Session, email: Annotated[str, Query(min_length=5, max_length=320)]) -> dict:
    return {"deleted_conversations": await delete_customer_data(session, email)}
