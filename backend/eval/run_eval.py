"""Eval: 60 in-KB questions (20 per DE/EN/NL) + 10 out-of-KB questions (hallucination check).

    docker compose --profile eval run --rm eval

in-KB pass = the expected document is cited AND (for a real LLM) an expected keyword appears in the answer.
out-of-KB pass = the bot says it has no answer (flag or wording) instead of inventing something.
With LLM_PROVIDER=fake only retrieval/routing is meaningful: the keyword check is reported but not gated.
"""

import asyncio
import json
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent.graph import GraphDeps, build_graph
from app.agent.llm import build_chat_model
from app.agent.sources import select_cited
from app.agent.tools import ToolContext
from app.agent.usage import UsageCollector, cost_eur
from app.config import get_settings
from app.integrations.freshdesk import FreshdeskClient
from app.integrations.shopify import ShopifyClient
from app.rag.embeddings import get_embedder
from app.services.seed import seed_demo_kb

QUESTIONS = Path(__file__).with_name("questions.jsonl")
REPORTS = Path("/app/reports")
PASS_THRESHOLD = 0.85
NO_ANSWER_RE = re.compile(
    r"couldn'?t find|could not find|don'?t have|do not have|no information|not (?:listed|available in)|colleague|"
    r"nicht gefunden|nichts gefunden|keine information|kollegen|niets gevonden|geen informatie|vind ik niets|collega", re.I)


async def main() -> int:
    s = get_settings()
    engine = create_async_engine(s.database_url, poolclass=NullPool)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    shopify, freshdesk = ShopifyClient(s), FreshdeskClient(s)
    async with sm() as session:
        await seed_demo_kb(session)

    llm, embedder = build_chat_model(s), get_embedder()
    deps = GraphDeps(s, llm, build_chat_model(s, summary=True), ToolContext(s, sm, embedder, shopify), freshdesk)
    graph = build_graph(InMemorySaver())
    questions = [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]
    real_llm = s.llm_provider != "fake"

    rows, total_cost = [], 0.0
    for q in questions:
        collector = UsageCollector()
        cfg = {"configurable": {"thread_id": str(uuid.uuid4()), "deps": deps}, "callbacks": [collector]}
        out = await graph.ainvoke({"messages": [HumanMessage(content=q["question"])]}, cfg)
        answer = next((m.content for m in reversed(out["messages"]) if isinstance(m, AIMessage)), "")
        answer = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        # strict: only sources the answer cites count (F2: "в каждом ответе ссылка на источник")
        refs = [x["ref"] for x in select_cited(answer, out.get("turn_sources", []), fallback=False)]
        cost = float(sum(cost_eur(r, s) for r in collector.records))
        total_cost += cost
        if q["type"] == "in_kb":
            src_ok = any(src in refs for src in q["sources"])
            retrieved = [x["ref"] for x in out.get("turn_sources", []) if x.get("type") != "ticket"]
            retr_ok = any(src in retrieved for src in q["sources"])
            kw_ok = any(k.lower() in answer.lower() for k in q["keywords"])
            # real LLM: the answer must cite the right document and contain the fact. fake LLM (quotes the first excerpt
            # only, cannot choose): the right document must at least have been retrieved.
            passed = (src_ok and kw_ok) if real_llm else retr_ok
            rows.append({**q, "answer": answer, "cited": refs, "retrieved": retrieved, "source_ok": src_ok, "retrieved_ok": retr_ok,
                         "keyword_ok": kw_ok, "pass": passed, "cost_eur": cost})
        else:
            said_no = bool(out.get("turn_no_answer")) or bool(NO_ANSWER_RE.search(answer))
            rows.append({**q, "answer": answer, "cited": refs, "pass": said_no, "cost_eur": cost})

    in_kb = [r for r in rows if r["type"] == "in_kb"]
    oos = [r for r in rows if r["type"] == "out_of_kb"]
    by_lang = defaultdict(list)
    for r in in_kb:
        by_lang[r["lang"]].append(r["pass"])
    accuracy = sum(r["pass"] for r in in_kb) / len(in_kb)
    source_acc = sum(r["source_ok"] for r in in_kb) / len(in_kb)
    halluc = [r for r in oos if not r["pass"]]
    # A rule-based fake LLM quotes whatever it retrieves, so only clearly unrelated questions can be gated offline.
    gated = halluc if real_llm else [r for r in halluc if r.get("kind") == "far"]
    ok = accuracy >= PASS_THRESHOLD and not gated

    lines = [
        f"# Eval report {datetime.now():%Y-%m-%d %H:%M}",
        f"- LLM: `{s.llm_provider}` / `{s.llm_model}`; embeddings: `{s.embeddings_provider}` / `{s.embeddings_model}`; reranker: `{s.reranker}`",
        f"- **In-KB accuracy: {accuracy:.1%}** ({sum(r['pass'] for r in in_kb)}/{len(in_kb)}), target >= {PASS_THRESHOLD:.0%}"
        + ("" if real_llm else " (fake LLM: gated on 'right document retrieved in top-k'; citation quality needs a real model)"),
        f"- Correct source cited: {source_acc:.1%}; right document retrieved in top-{s.retrieval_top_k}: {sum(r['retrieved_ok'] for r in in_kb) / len(in_kb):.1%}",
        *[f"  - {lang.upper()}: {sum(v)}/{len(v)}" for lang, v in sorted(by_lang.items())],
        f"- **Out-of-KB: {len(oos) - len(halluc)}/{len(oos)} honest 'no answer'**, target 10/10"
        + ("" if real_llm else f" (fake LLM: gated on the {sum(r.get('kind') == 'far' for r in oos)} unrelated questions only)"),
        f"- LLM cost of the run: EUR {total_cost:.4f} ({total_cost / len(rows):.5f} per question)",
        f"- Result: **{'PASS' if ok else 'FAIL'}**", "", "## Failures",
    ]
    for r in [*(r for r in in_kb if not r["pass"]), *halluc]:
        lines += [f"- `{r['id']}` [{r['type']}] {r['question']}", f"  - expected one of: {r.get('sources')} cited: {r['cited']}", f"  - answer: {r['answer'][:300]!r}"]
    text = "\n".join(lines)
    print(text)
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (REPORTS / f"eval-{stamp}.md").write_text(text, encoding="utf-8")
    (REPORTS / f"eval-{stamp}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    await engine.dispose()
    await shopify.aclose()
    await freshdesk.aclose()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
