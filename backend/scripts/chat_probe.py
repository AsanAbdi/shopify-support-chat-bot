"""Talk to a running API over WebSocket from the command line (one conversation, messages in order).

    docker compose run --rm --no-deps -e PROBE_URL=ws://api:8000/ws/chat api python -m scripts.chat_probe "msg 1" "msg 2"
"""

import asyncio
import json
import os
import sys
import time

from websockets.asyncio.client import connect

URL = os.environ.get("PROBE_URL", "ws://api:8000/ws/chat")


async def main(messages: list[str]) -> int:
    failures = 0
    async with connect(URL, open_timeout=15) as ws:
        for text in messages:
            started = time.monotonic()
            await ws.send(json.dumps({"type": "message", "text": text}))
            answer, sources, first, done = "", [], None, {}
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
                if ev["type"] == "token":
                    first = first if first is not None else time.monotonic() - started
                    answer += ev["text"]
                elif ev["type"] == "reset":
                    answer = ""
                elif ev["type"] == "replace":
                    answer = ev["text"]
                elif ev["type"] == "sources":
                    sources = [f"[{s['n']}] {s['title']}" for s in ev["items"]]
                elif ev["type"] in {"done", "error"}:
                    done = ev
                    break
            failures += done["type"] == "error"
            print(f"\n>>> {text}")
            print(f"<<< {answer.strip()}")
            print(f"    ttft={first and round(first, 2)}s total={round(time.monotonic() - started, 2)}s sources={sources} "
                  f"escalated={done.get('escalated')} ticket={done.get('ticket_id')} {'ERROR ' + done.get('code', '') if done['type'] == 'error' else ''}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
