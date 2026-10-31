"""Load test: N concurrent WebSocket dialogs against a running API (docker compose --profile load run --rm loadtest).

Measures time-to-first-token and full-turn latency per message. With LLM_PROVIDER=fake this isolates our own stack
(embeddings, Postgres retrieval, LangGraph, streaming); real-LLM latency is added on top and must be measured with a key.
"""

import asyncio
import json
import os
import random
import ssl
import statistics
import sys
import time
from pathlib import Path

from websockets.asyncio.client import connect

URL = os.environ.get("LOADTEST_URL", "ws://api-load:8000/ws/chat")
USERS = int(os.environ.get("LOADTEST_USERS", "30"))
TURNS = int(os.environ.get("LOADTEST_TURNS", "4"))
ORIGIN = os.environ.get("LOADTEST_ORIGIN")  # Origin header, to exercise the CORS allow-list
INSECURE_TLS = os.environ.get("LOADTEST_INSECURE") == "1"  # self-signed rehearsal certs only
SNI = os.environ.get("LOADTEST_SNI")
TTFT_TARGET_S = float(os.environ.get("LOADTEST_TTFT_P95_S", "2.5"))
ORDER_TARGET_S = float(os.environ.get("LOADTEST_ORDER_S", "4.0"))

QUESTIONS = [
    ("kb", "How many days do I have to return an item?"),
    ("kb", "Was kostet der Versand nach Deutschland?"),
    ("kb", "Welke betaalmethoden accepteren jullie?"),
    ("kb", "How do I wash my down jacket?"),
    ("product", "Do you have an Alpine Shell Jacket?"),
    ("order", "Where is my order 1002? My email is jan.devries@example.nl"),
    ("order", "Order 1001, anna.schmidt@example.de"),
    ("none", "What will the weather be like in Berlin tomorrow today?"),
]


async def one_turn(ws, text: str) -> tuple[float | None, float, str | None]:
    started = time.monotonic()
    await ws.send(json.dumps({"type": "message", "text": text}))
    first: float | None = None
    while True:
        ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if ev["type"] == "token" and first is None:
            first = time.monotonic() - started
        if ev["type"] == "done":
            return first, time.monotonic() - started, None
        if ev["type"] == "error":
            return first, time.monotonic() - started, ev.get("code", "error")


async def user(idx: int, results: list[dict]) -> None:
    rnd = random.Random(idx)
    try:
        kwargs: dict = {"open_timeout": 15, "origin": ORIGIN}
        if URL.startswith("wss"):
            ctx = ssl.create_default_context()
            if INSECURE_TLS:
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            kwargs.update(ssl=ctx, server_hostname=SNI)
        async with connect(URL, **kwargs) as ws:
            for _ in range(TURNS):
                kind, text = rnd.choice(QUESTIONS)
                first, total, err = await one_turn(ws, text)
                results.append({"user": idx, "kind": kind, "first": first, "total": total, "error": err})
                await asyncio.sleep(rnd.uniform(0.3, 1.5))  # think time
    except Exception as exc:
        results.append({"user": idx, "kind": "conn", "first": None, "total": 0.0, "error": f"{type(exc).__name__}: {exc}"})


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))] if s else float("nan")


async def main() -> int:
    results: list[dict] = []
    t0 = time.monotonic()
    await asyncio.gather(*(user(i, results) for i in range(USERS)))
    wall = time.monotonic() - t0

    ok = [r for r in results if not r["error"] and r["first"] is not None]
    errors = [r for r in results if r["error"]]
    first = [r["first"] for r in ok]
    order_totals = [r["total"] for r in ok if r["kind"] == "order"]
    report = {
        "users": USERS, "turns": len(results), "errors": len(errors), "wall_s": round(wall, 1),
        "ttft_p50_s": round(statistics.median(first), 3) if first else None, "ttft_p95_s": round(pct(first, 95), 3),
        "ttft_max_s": round(max(first), 3) if first else None,
        "order_total_p95_s": round(pct(order_totals, 95), 3) if order_totals else None,
        "error_samples": sorted({e["error"] for e in errors})[:5],
    }
    print(json.dumps(report, indent=1))
    out = Path("/app/reports")
    if out.exists():
        (out / f"loadtest-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps({"summary": report, "raw": results}, indent=1))
    passed = not errors and report["ttft_p95_s"] < TTFT_TARGET_S and (not order_totals or report["order_total_p95_s"] < ORDER_TARGET_S)
    print("PASS" if passed else "FAIL", f"(targets: TTFT p95 < {TTFT_TARGET_S}s, order turn p95 < {ORDER_TARGET_S}s, 0 errors)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
