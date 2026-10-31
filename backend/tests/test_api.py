"""HTTP/WebSocket level tests. Sync TestClient: the app runs in its own event loop/thread with its own DB pool."""

import asyncio

import pytest
import redis as sync_redis
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent.prompts import canary
from app.config import get_settings
from app.main import create_app
from app.services.leak_guard import LeakGuard
from app.services.seed import seed_demo_kb
from tests.conftest import FIXTURE_KB, TEST_URL


def run_db(coro_fn):
    async def go():
        eng = create_async_engine(TEST_URL, poolclass=NullPool)
        try:
            async with async_sessionmaker(eng, expire_on_commit=False)() as s:
                result = await coro_fn(s)
                await s.commit()
                return result
        finally:
            await eng.dispose()

    return asyncio.run(go())


@pytest.fixture
def client(monkeypatch):
    # Celery is not part of API tests: capture the enqueue calls instead of talking to a broker.
    queued = []
    from app.api import admin

    monkeypatch.setattr(admin.index_document_task, "delay", lambda *a, **k: queued.append(a))
    reindex_all_calls = []
    monkeypatch.setattr(admin.reindex_all_task, "delay", lambda *a, **k: reindex_all_calls.append(a))
    sync_redis.from_url(get_settings().redis_url).flushdb()  # reset rate-limit counters
    run_db(lambda s: s.execute(text("TRUNCATE users, chunks, documents, messages, conversations, llm_usage, unanswered_questions RESTART IDENTITY CASCADE")))
    run_db(lambda s: seed_demo_kb(s, FIXTURE_KB))
    with TestClient(create_app()) as c:
        c.queued = queued
        c.reindex_all_calls = reindex_all_calls
        yield c


def _login(client, username="admin", password="admin-test-pass-1") -> dict:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _chat(client, text_: str, session_id: str | None = None) -> list[dict]:
    events = []
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "message", "text": text_, **({"session_id": session_id} if session_id else {})})
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in {"done", "error"}:
                return events


def test_health_and_widget_are_served(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
    js = client.get("/widget.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert client.get("/metrics").status_code == 200


def test_ws_chat_streams_tokens_then_sources_then_done(client):
    events = _chat(client, "How many days do I have to return an item?")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "session" and kinds.count("token") > 3 and kinds[-1] == "done"
    assert kinds.index("sources") < kinds.index("done")
    answer = "".join(e["text"] for e in events if e["type"] == "token")
    assert answer.strip()
    sources = next(e for e in events if e["type"] == "sources")["items"]
    assert sources[0]["title"] and sources[0]["url"].endswith("/file")  # file sources link to the public download
    assert events[-1]["message_id"]


def test_session_survives_reconnect_and_history_is_returned(client):
    first = _chat(client, "How many days do I have to return an item?")
    sid = first[0]["id"]
    second = _chat(client, "And what about the refund?", session_id=sid)
    assert second[0]["type"] != "session" or second[0]["id"] == sid  # same conversation, no new session announced
    hist = client.get("/api/chat/history", params={"session_id": sid}).json()
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant", "user", "assistant"]


def test_expired_session_starts_a_new_conversation(client):
    sid = _chat(client, "How many days do I have to return an item?")[0]["id"]
    run_db(lambda s: s.execute(text("UPDATE conversations SET last_activity_at = now() - interval '25 hours'")))
    run_db(lambda s: s.commit())
    assert client.get("/api/chat/history", params={"session_id": sid}).status_code == 404
    events = _chat(client, "hello again, how many days for returns?", session_id=sid)
    assert events[0]["type"] == "session" and events[0]["id"] != sid


def test_feedback_is_stored_and_flags_the_conversation(client):
    events = _chat(client, "How many days do I have to return an item?")
    sid, mid = events[0]["id"], events[-1]["message_id"]
    assert client.post("/api/chat/feedback", json={"session_id": sid, "message_id": mid, "value": -1}).status_code == 200
    assert client.post("/api/chat/feedback", json={"session_id": sid, "message_id": mid, "value": 7}).status_code == 400
    assert client.post("/api/chat/feedback", json={"session_id": "other", "message_id": mid, "value": 1}).status_code == 400
    headers = _login(client)
    items = client.get("/api/admin/conversations", params={"filter": "downvoted"}, headers=headers).json()["items"]
    assert [i["id"] for i in items] == [sid]


def test_chat_rate_limit(client):
    limit = client.app.state.container.settings.rate_limit_chat_per_min
    with client.websocket_connect("/ws/chat") as ws:
        errors = 0
        for _ in range(limit + 3):
            ws.send_json({"type": "message", "text": "x"})
            while True:
                ev = ws.receive_json()
                if ev["type"] in {"done", "error"}:
                    errors += ev["type"] == "error" and ev.get("code") == "rate_limited"
                    break
        assert errors >= 3


def test_message_length_limit(client):
    events = _chat(client, "x" * 2500)
    assert events[-1]["type"] == "error" and events[-1]["code"] == "invalid_message"


def test_sse_fallback(client):
    res = client.post("/api/chat/stream", json={"text": "How many days do I have to return an item?"})
    assert res.status_code == 200 and "event-stream" in res.headers["content-type"]
    assert '"type": "done"' in res.text and '"type": "token"' in res.text


def test_escalation_is_visible_in_admin_with_summary(client):
    events = _chat(client, "I want to speak to a human, my email is buyer@example.org")
    assert events[-1]["escalated"] and events[-1]["ticket_id"]
    headers = _login(client)
    items = client.get("/api/admin/conversations", params={"filter": "escalated"}, headers=headers).json()["items"]
    assert items[0]["email"] == "buyer@example.org" and items[0]["reason"] == "human_request"
    detail = client.get(f"/api/admin/conversations/{items[0]['id']}", headers=headers).json()
    assert detail["ticket_summary"] and detail["messages"][0]["role"] == "user"


def test_unanswered_questions_show_up_in_stats(client):
    _chat(client, "What will the weather be like in Berlin tomorrow today?")
    stats = client.get("/api/admin/stats", headers=_login(client)).json()
    assert stats["top_unanswered"][0]["count"] == 1 and "weather" in stats["top_unanswered"][0]["question"]
    assert stats["conversations_total"] == 1 and stats["closed_without_operator_pct"] == 100.0
    assert stats["llm_cost_eur_month"] == 0  # the fake model is free: it must not consume the real monthly budget
    assert run_db(lambda s: s.execute(text("SELECT count(*), sum(input_tokens) FROM llm_usage"))).one()[1] > 0  # but usage is still recorded


def test_budget_hard_stop_blocks_llm_calls(client):
    run_db(lambda s: s.execute(text("INSERT INTO llm_usage (model, input_tokens, output_tokens, cost_eur) VALUES ('x', 1, 1, 5000)")))
    run_db(lambda s: s.commit())
    events = _chat(client, "How many days do I have to return an item?")
    assert "temporarily unavailable" in "".join(e.get("text", "") for e in events)


def test_admin_requires_auth_and_roles(client):
    assert client.get("/api/admin/documents").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    admin_h = _login(client)
    assert client.post("/api/admin/users", headers=admin_h, json={"username": "agent1", "password": "agent-pass-1234", "role": "agent"}).status_code == 201
    agent_h = _login(client, "agent1", "agent-pass-1234")
    assert client.get("/api/admin/documents", headers=agent_h).status_code == 200  # agents can look
    assert client.get("/api/admin/conversations", headers=agent_h).status_code == 200
    assert client.post("/api/admin/documents", headers=agent_h, files={"file": ("a.md", b"# A\ntext text text text text")}).status_code == 403
    assert client.delete("/api/admin/documents/1", headers=agent_h).status_code == 403
    assert client.get("/api/admin/users", headers=agent_h).status_code == 403
    assert client.delete("/api/admin/gdpr/customer", params={"email": "a@b.co"}, headers=agent_h).status_code == 403


def test_document_upload_validation_and_enqueue(client, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    h = _login(client)
    assert client.post("/api/admin/documents", headers=h, files={"file": ("evil.exe", b"MZ")}).status_code == 415
    res = client.post("/api/admin/documents", headers=h, files={"file": ("faq.md", b"# FAQ\n\nWe ship worldwide on Mondays only.")})
    assert res.status_code == 202 and res.json()["status"] == "pending"
    assert client.queued and client.queued[-1][0] == res.json()["id"]
    again = client.post("/api/admin/documents", headers=h, files={"file": ("faq.md", b"# FAQ\n\nWe ship worldwide on Tuesdays only.")})
    assert again.status_code == 202 and again.json()["id"] == res.json()["id"]  # same filename = new version, same document
    assert client.post(f"/api/admin/documents/{res.json()['id']}/reindex", headers=h).status_code == 202
    assert client.post("/api/admin/documents/url", headers=h, json={"url": "https://help.example.com/a"}).status_code == 202
    docs = client.get("/api/admin/documents", headers=h).json()
    assert {d["source"] for d in docs} >= {"faq.md", "https://help.example.com/a", "returns.md"}
    assert client.delete(f"/api/admin/documents/{res.json()['id']}", headers=h).status_code == 204


def test_gdpr_erasure_removes_conversations_and_checkpoints(client):
    events = _chat(client, "I want to speak to a human, my email is erase.me@example.org")
    sid = events[0]["id"]
    h = _login(client)
    counts = lambda: run_db(lambda s: s.execute(text("SELECT (SELECT count(*) FROM conversations), (SELECT count(*) FROM checkpoints WHERE thread_id=:t)"), {"t": sid}))  # noqa: E731
    assert tuple(counts().one()) == (1, pytest.approx(counts().one()[1]))
    assert counts().one()[1] > 0
    res = client.delete("/api/admin/gdpr/customer", params={"email": "Erase.Me@example.org"}, headers=h)
    assert res.json() == {"deleted_conversations": 1}
    assert tuple(counts().one()) == (0, 0)
    assert client.get("/api/chat/history", params={"session_id": sid}).status_code == 404


def test_retention_purges_old_conversations(client):
    sid = _chat(client, "How many days do I have to return an item?")[0]["id"]
    run_db(lambda s: s.execute(text("UPDATE conversations SET last_activity_at = now() - interval '91 days'")))
    run_db(lambda s: s.commit())
    from app.services.gdpr import purge_expired

    assert run_db(lambda s: purge_expired(s, 90)) == 1
    assert run_db(lambda s: s.execute(text("SELECT count(*) FROM messages WHERE conversation_id=:c"), {"c": sid})).scalar() == 0


def test_leak_guard_catches_a_canary_split_across_stream_chunks():
    guard, emitted = LeakGuard(), ""
    secret = canary()
    for piece in ["Sure, the marker is ", secret[:5], secret[5:9], secret[9:], " and more text"]:
        emitted += guard.feed(piece)
    emitted += guard.flush()
    assert guard.blocked and secret not in emitted and secret[:8] not in emitted


def test_leak_guard_passes_normal_text_unchanged():
    guard, out = LeakGuard(), ""
    for piece in ["Returns are ", "possible within ", "30 days."]:
        out += guard.feed(piece)
    assert out + guard.flush() == "Returns are possible within 30 days." and not guard.blocked


def test_user_lifecycle_change_password_disable_delete(client):
    admin_h = _login(client)
    created = client.post("/api/admin/users", headers=admin_h, json={"username": "agent2", "password": "agent-pass-1234", "role": "agent"})
    assert created.status_code == 201
    uid = next(u["id"] for u in client.get("/api/admin/users", headers=admin_h).json() if u["username"] == "agent2")
    agent_h = _login(client, "agent2", "agent-pass-1234")

    # own password change: wrong current password is refused, then it works and the old one stops working
    bad = client.post("/api/auth/change-password", headers=agent_h, json={"old_password": "nope-nope-nope", "new_password": "new-agent-pass-1"})
    assert bad.status_code == 403
    assert client.post("/api/auth/change-password", headers=agent_h, json={"old_password": "agent-pass-1234", "new_password": "new-agent-pass-1"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "agent2", "password": "agent-pass-1234"}).status_code == 401
    agent_h = _login(client, "agent2", "new-agent-pass-1")

    # admin resets the password, promotes, then disables: the already-issued token dies immediately
    assert client.patch(f"/api/admin/users/{uid}", headers=admin_h, json={"password": "reset-by-admin-1"}).status_code == 200
    assert client.patch(f"/api/admin/users/{uid}", headers=admin_h, json={"role": "admin"}).json()["role"] == "admin"
    assert client.get("/api/admin/documents", headers=agent_h).status_code == 200
    assert client.patch(f"/api/admin/users/{uid}", headers=admin_h, json={"is_active": False}).json()["is_active"] is False
    assert client.get("/api/admin/documents", headers=agent_h).status_code == 401
    assert client.post("/api/auth/login", json={"username": "agent2", "password": "reset-by-admin-1"}).status_code == 401

    assert client.delete(f"/api/admin/users/{uid}", headers=admin_h).status_code == 204
    assert client.delete(f"/api/admin/users/{uid}", headers=admin_h).status_code == 404


def test_the_last_active_admin_cannot_be_removed(client):
    admin_h = _login(client)
    me_id = next(u["id"] for u in client.get("/api/admin/users", headers=admin_h).json() if u["username"] == "admin")
    assert client.patch(f"/api/admin/users/{me_id}", headers=admin_h, json={"role": "agent"}).status_code == 409
    assert client.patch(f"/api/admin/users/{me_id}", headers=admin_h, json={"is_active": False}).status_code == 409
    assert client.delete(f"/api/admin/users/{me_id}", headers=admin_h).status_code == 409


def test_agents_cannot_manage_users(client):
    admin_h = _login(client)
    client.post("/api/admin/users", headers=admin_h, json={"username": "agent3", "password": "agent-pass-1234", "role": "agent"})
    agent_h = _login(client, "agent3", "agent-pass-1234")
    assert client.patch("/api/admin/users/1", headers=agent_h, json={"role": "admin"}).status_code == 403
    assert client.delete("/api/admin/users/1", headers=agent_h).status_code == 403


def test_websocket_origin_is_enforced_when_cors_is_restricted(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(client.app.state.container.settings, "cors_origins", ["https://shop.example"])
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/ws/chat", headers={"origin": "https://evil.example"}):
        pass
    assert exc.value.code == 4403
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/chat"):  # no Origin header at all
        pass
    with client.websocket_connect("/ws/chat", headers={"origin": "https://shop.example"}) as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_reindex_all_is_admin_only_marks_documents_pending_and_queues_one_job(client):
    admin_h = _login(client)
    client.post("/api/admin/users", headers=admin_h, json={"username": "agent9", "password": "agent-pass-1234", "role": "agent"})
    agent_h = _login(client, "agent9", "agent-pass-1234")
    assert client.post("/api/admin/reindex-all", headers=agent_h).status_code == 403
    assert client.post("/api/admin/reindex-all").status_code == 401
    assert client.reindex_all_calls == []

    before = client.get("/api/admin/documents", headers=admin_h).json()
    assert before and {d["status"] for d in before} == {"indexed"}
    res = client.post("/api/admin/reindex-all", headers=admin_h)
    assert res.status_code == 202 and res.json() == {"queued": True, "documents": len(before)}
    assert len(client.reindex_all_calls) == 1  # a single background job, not one per document
    after = client.get("/api/admin/documents", headers=admin_h).json()
    assert {d["status"] for d in after} == {"pending"}  # the admin page shows progress right away


def test_conversation_list_has_previews_counters_search_and_pagination(client):
    _chat(client, "How many days do I have to return an item?")
    _chat(client, "I want to speak to a human, my email is buyer@example.org")
    _chat(client, "What will the weather be like in Berlin tomorrow today?")
    h = _login(client)
    url = "/api/admin/conversations"
    data = client.get(url, headers=h).json()
    assert data["total"] == 3 and data["counts"] == {"all": 3, "escalated": 1, "downvoted": 0, "no_answer": 1}
    newest = data["items"][0]
    assert newest["preview"].startswith("What will the weather") and newest["messages"] == 2  # list rows need no extra request

    esc = client.get(url, params={"filter": "escalated"}, headers=h).json()
    assert esc["total"] == 1 and esc["items"][0]["email"] == "buyer@example.org"

    assert client.get(url, params={"q": "buyer@"}, headers=h).json()["total"] == 1  # email
    assert client.get(url, params={"q": "weather"}, headers=h).json()["total"] == 1  # text of a message
    oldest_id = data["items"][-1]["id"]
    assert [i["id"] for i in client.get(url, params={"q": oldest_id[:8]}, headers=h).json()["items"]] == [oldest_id]
    assert client.get(url, params={"q": "zzz-nothing"}, headers=h).json()["total"] == 0
    assert client.get(url, params={"q": "%"}, headers=h).json()["total"] == 0  # % and _ are literal, not wildcards

    page = client.get(url, params={"limit": 2, "offset": 2}, headers=h).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    combined = client.get(url, params={"q": "buyer@", "filter": "no_answer"}, headers=h).json()
    assert combined["counts"]["all"] == 1 and combined["total"] == 0  # pill counters follow the search box, the total follows the pill

    detail = client.get(f"{url}/{newest['id']}", headers=h).json()
    assert detail["created_at"] and detail["last_activity_at"] and [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_admin_can_delete_one_conversation_with_its_checkpoint_agents_cannot(client):
    sid = _chat(client, "How many days do I have to return an item?")[0]["id"]
    admin_h = _login(client)
    client.post("/api/admin/users", headers=admin_h, json={"username": "agent8", "password": "agent-pass-1234", "role": "agent"})
    agent_h = _login(client, "agent8", "agent-pass-1234")
    checkpoints = lambda: run_db(lambda s: s.execute(text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"), {"t": sid})).scalar()  # noqa: E731
    assert checkpoints() > 0

    assert client.delete(f"/api/admin/conversations/{sid}", headers=agent_h).status_code == 403
    assert client.delete("/api/admin/conversations/00000000-nope", headers=admin_h).status_code == 404
    assert client.delete(f"/api/admin/conversations/{sid}", headers=admin_h).status_code == 204
    assert client.get(f"/api/admin/conversations/{sid}", headers=admin_h).status_code == 404
    assert client.get("/api/chat/history", params={"session_id": sid}).status_code == 404
    assert checkpoints() == 0
    assert run_db(lambda s: s.execute(text("SELECT count(*) FROM messages WHERE conversation_id = :c"), {"c": sid})).scalar() == 0
