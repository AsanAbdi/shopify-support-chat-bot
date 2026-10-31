"""Offline lab: compare embedding models / the reranker on the eval questions without touching the database.

    docker compose run --rm -e LAB_MODELS=... lab

For every model it reports top-1/top-3 source accuracy on the in-KB questions and how well a similarity threshold
separates in-KB questions from out-of-KB ones (the gate in front of the LLM). Pure-vector ranking; production
adds Postgres full-text via RRF, so absolute numbers differ slightly but model ranking carries over.
"""

import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.rag.chunking import chunk_document
from app.rag.loaders import load_file

SEED = Path(__file__).resolve().parents[1] / "seed" / "kb"
QUESTIONS = Path(__file__).resolve().parents[1] / "eval" / "questions.jsonl"
CACHE = os.environ.get("LAB_CACHE", "/lab-models")

# name -> (fastembed model, query prefix, passage prefix)
MODELS = {
    "minilm": ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "", ""),
    "mpnet": ("sentence-transformers/paraphrase-multilingual-mpnet-base-v2", "", ""),
    "e5-large": ("intfloat/multilingual-e5-large", "query: ", "passage: "),
}
RERANKER = "jinaai/jina-reranker-v2-base-multilingual"


def load_corpus() -> tuple[list[str], list[str]]:
    s = get_settings()
    texts, sources = [], []
    for path in sorted(SEED.iterdir()):
        for c in chunk_document(load_file(path.read_bytes(), path.name), chunk_size=s.chunk_size, chunk_overlap=s.chunk_overlap):
            texts.append(c.content)
            sources.append(path.name)
    return texts, sources


def embed(model_id: str, texts: list[str]) -> np.ndarray:
    from fastembed import TextEmbedding

    cache = "/opt/models" if "MiniLM" in model_id else CACHE
    m = TextEmbedding(model_id, cache_dir=cache)
    return np.array(list(m.embed(texts, batch_size=16)))


def normalise(a: np.ndarray) -> np.ndarray:
    return a / np.linalg.norm(a, axis=1, keepdims=True)


def sweep(in_scores: list[float], in_ok: list[bool], oos_scores: list[float]) -> tuple[float, float, float, float]:
    best = (0.0, 0.0, 0.0, 0.0)
    for t in np.arange(0.05, 0.95, 0.01):
        kept = sum(ok and sc >= t for ok, sc in zip(in_ok, in_scores, strict=True)) / len(in_ok)
        rej = sum(sc < t for sc in oos_scores) / len(oos_scores)
        total = (kept * len(in_ok) + rej * len(oos_scores)) / (len(in_ok) + len(oos_scores))
        if total > best[0]:
            best = (total, float(t), kept, rej)
    return best


def main() -> int:
    questions = [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]
    texts, sources = load_corpus()
    in_q = [q for q in questions if q["type"] == "in_kb"]
    oos_q = [q for q in questions if q["type"] == "out_of_kb"]
    print(f"corpus: {len(texts)} chunks from {len(set(sources))} files; {len(in_q)} in-KB and {len(oos_q)} out-of-KB questions")
    wanted = os.environ.get("LAB_MODELS", "minilm").split(",")
    results = {}
    for name in wanted:
        model_id, qp, pp = MODELS[name]
        t0 = time.time()
        corpus = normalise(embed(model_id, [pp + t for t in texts]))
        in_vecs = normalise(embed(model_id, [qp + q["question"] for q in in_q]))
        oos_vecs = normalise(embed(model_id, [qp + q["question"] for q in oos_q]))
        sims_in, sims_oos = in_vecs @ corpus.T, oos_vecs @ corpus.T
        top1 = [sources[int(np.argmax(r))] in q["sources"] for r, q in zip(sims_in, in_q, strict=True)]
        top3 = [bool(set(q["sources"]) & {sources[i] for i in np.argsort(-r)[:3]}) for r, q in zip(sims_in, in_q, strict=True)]
        by_lang = {lang: sum(ok for ok, q in zip(top1, in_q, strict=True) if q["lang"] == lang) for lang in ("de", "en", "nl")}
        total, thr, kept, rej = sweep([float(r.max()) for r in sims_in], top1, [float(r.max()) for r in sims_oos])
        results[name] = {"top1": sum(top1) / len(top1), "top3": sum(top3) / len(top3), "by_lang_top1": by_lang,
                         "best_threshold": round(thr, 2), "in_kb_kept": round(kept, 3), "oos_rejected": round(rej, 3),
                         "combined": round(total, 3), "seconds": round(time.time() - t0, 1)}
        print(name, json.dumps(results[name]))

        if os.environ.get("LAB_RERANK") == "1":
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            ce = TextCrossEncoder(RERANKER, cache_dir=CACHE)

            def rerank(qs: list[dict], sims: np.ndarray, ce=ce) -> list[tuple[str, float]]:
                out = []
                for q, row in zip(qs, sims, strict=True):
                    idx = list(np.argsort(-row)[:8])
                    scores = list(ce.rerank(q["question"], [texts[i] for i in idx]))
                    j = int(np.argmax(scores))
                    out.append((sources[idx[j]], 1 / (1 + math.exp(-float(scores[j])))))
                return out

            r_in, r_oos = rerank(in_q, sims_in), rerank(oos_q, sims_oos)
            ok = [src in q["sources"] for (src, _), q in zip(r_in, in_q, strict=True)]
            total, thr, kept, rej = sweep([s for _, s in r_in], ok, [s for _, s in r_oos])
            results[name + "+rerank"] = {"top1": sum(ok) / len(ok), "best_threshold": round(thr, 2),
                                         "in_kb_kept": round(kept, 3), "oos_rejected": round(rej, 3), "combined": round(total, 3)}
            print(name + "+rerank", json.dumps(results[name + "+rerank"]))
    Path("/app/reports").mkdir(exist_ok=True)
    Path("/app/reports/retrieval-lab.json").write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
