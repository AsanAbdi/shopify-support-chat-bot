"""Calibrate the "nothing found" gate on the real pipeline (hybrid retrieval, current embedder) with the eval questions.

    docker compose run --rm --no-deps -e LLM_PROVIDER=fake api python -m scripts.calibrate

Prints the similarity distribution of in-KB vs out-of-KB questions and the threshold sweep. No LLM calls.
"""

import asyncio
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag.embeddings import get_embedder
from app.rag.retriever import confidence, retrieve

QUESTIONS = Path(__file__).resolve().parents[1] / "eval" / "questions.jsonl"


async def main() -> None:
    s = get_settings()
    engine = create_async_engine(s.database_url, poolclass=NullPool)
    rows = [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]
    inkb, oos, miss_rank = [], [], []
    async with async_sessionmaker(engine)() as session:
        for q in rows:
            hits = await retrieve(session, get_embedder(), q["question"], settings=s)
            c = confidence(hits)
            if q["type"] == "in_kb":
                refs = [h.source for h in hits]
                found = any(src in refs for src in q["sources"])
                inkb.append((c, found, q["id"]))
                if not found:
                    miss_rank.append((q["id"], refs[:3]))
            else:
                oos.append((c, q["kind"], q["id"]))
    await engine.dispose()
    in_conf = sorted(c for c, _, _ in inkb)
    print(f"in-KB confidence min={in_conf[0]:.3f} p10={in_conf[len(in_conf) // 10]:.3f} median={in_conf[len(in_conf) // 2]:.3f}")
    print(f"retrieved the right document in top-{s.retrieval_top_k}: {sum(f for _, f, _ in inkb)}/{len(inkb)}")
    print("out-of-KB confidence:", sorted((round(c, 3), kind) for c, kind, _ in oos))
    print("not found in top-k:", miss_rank)
    best = (0.0, 0.0)
    for t in [x / 1000 for x in range(600, 900, 5)]:
        kept = sum(c >= t and f for c, f, _ in inkb) / len(inkb)
        rej = sum(c < t for c, _, _ in oos) / len(oos)
        print(f"  threshold {t:.3f}: in-KB answered {kept:.0%}, out-of-KB rejected {rej:.0%}")
        best = max(best, (kept + rej, t))
    print("best (kept+rejected):", best)


if __name__ == "__main__":
    asyncio.run(main())
