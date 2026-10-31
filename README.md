# AI Support Assistant for Shopify Stores

A personal project for exploring LangChain and LangGraph on a realistic problem: a customer support assistant for an
online store that answers from a knowledge base, checks order status, searches products and hands difficult cases to a human agent.

There is no real store behind it. Shopify and Freshdesk are replaced by a `mock-services` container, and the data is synthetic:
a fictional shop called Nordvik Outdoor with about 1,500 SKUs, 101 knowledge base documents in German, English and Dutch, and 20 orders
in different states. Everything else is built to production standards, with tests, measurements, monitoring, GDPR handling and AWS infrastructure.

**Stack:** Python 3.12, FastAPI, LangChain, LangGraph, Claude (Anthropic), PostgreSQL + pgvector, Celery + Redis, Docker Compose, Terraform.

## Features

- **Knowledge base answers** with source links: hybrid search (vector + full-text), streaming, German, English and Dutch.
- **Order status** by order number and email, with ownership verified in code rather than left to the model.
- **Product search** by price, size, color and availability.
- **Human handover** to Freshdesk with a summary and the conversation history.
- **Chat widget** in plain JavaScript, embedded with a single `<script>` tag, using WebSocket with an SSE fallback.
- **Admin panel:** conversations, statistics, LLM spending, knowledge base, users and roles.
- **Safeguards:** rate limiting, prompt injection detection, a canary against system prompt leaks and a monthly spending limit.
- **GDPR:** emails and phone numbers masked in logs, conversations deleted after 90 days or on request.
- **Operations:** Prometheus and Grafana, Sentry, Langfuse, Terraform for AWS, backups.
- **Shopify mock** that follows the real API: GraphQL, query costs, throttling, access errors and the 60-day order window.

```
widget.js ──WebSocket/SSE──► FastAPI ──► LangGraph: prepare → agent ⇄ tools → finalize
admin SPA ──REST+JWT───────►    │                      └→ escalate (Freshdesk ticket)
                                ├─ Postgres + pgvector: documents, chunks, catalog, conversations, graph checkpoints
                                ├─ Redis: rate limiting, conversation locks, Celery broker
                                └─ Celery: indexing, catalog sync, retention cleanup
```

## Targets and Results

| Metric | Target | Result | Measured with |
| --- | --- | --- | --- |
| Time to first token, knowledge base answer | under 2.5 s | 2.35 s | Claude Haiku 4.5, single turn |
| Time to first token, order lookup with a tool call | under 2.5 s | 2.36 s | Claude Haiku 4.5, single turn |
| p95 time to first token, 30 concurrent conversations | under 2.5 s | 0.9–1.4 s, excluding the model | fake model, 4 CPUs, 120 turns, 0 errors |
| Correct document in the top 5 search results | at least 85% | 100% | 60 questions, Claude Haiku 4.5 |
| Answer accuracy (right source cited and expected fact present) | at least 85% | 91.7% (EN 19/20, DE 19/20, NL 17/20) | 60 questions, Claude Haiku 4.5 |
| Recognized "no answer" on out-of-scope questions | 10 of 10 | **6 of 10, target missed** | 10 questions, Claude Haiku 4.5 |

### Eval Failures

Reading the remaining failures by hand shows where the strict automatic score comes from:

- **3 parcel replies:** for "my parcel has not arrived", the bot focuses on the order number and leaves out the 5-working-day guidance.
- **2 scoring artifacts:** the answer has the right fact but cites a document outside the expected list.
- **4 out-of-scope failures:** 3 are reasonable refusals in wording the checker does not recognize, and 1 states an unsupported "no" about 12-month installments.

The first run on this question set scored 80.0% in-scope accuracy and 88.3% search accuracy. The fixes are described below. Between the two runs the expected document lists were also extended with the new translations and documents that do contain the fact.

### Real Model Results

Cost and latency figures come from 25 calls in manual conversations, one full trace and the 70-question eval.

| Metric | Value |
| --- | --- |
| Average call | 2,666 input tokens, 104 output tokens, about €0.003 |
| Knowledge base question | 1 call, about €0.003, full answer in 3.1 s |
| Order question (tool call + answer) | 2 calls, about €0.007, full answer in 3.1 s |
| 70-question eval | €0.26 in total, about €0.0037 per question |

Costs assume €0.92 per million input tokens and €4.60 per million output tokens. Timings come from a single run, so they are not p95 values.
The system has not been load tested with the real model, so the 0.9–1.4 s result above describes this stack only.

To reproduce the eval with a real API key:

```bash
docker compose --profile eval-real run --rm eval-real
```

## How the Results Were Achieved

**Latency.** The first load test showed a p95 time to first token of 3.9 s with 30 conversations, because the embedding library used every core
for each request. Limiting ONNX threads, capping concurrent embedding work and caching repeated queries brought it down to 0.9–1.4 s.

**One model call for FAQ questions.** The graph searches the knowledge base before calling the model, and a confident match goes straight into the prompt.
That replaces two calls (decide to search, then answer) with one, saving both time and money.

**Search quality.** Embedding models were compared on the project's own questions: `multilingual-e5-large` reaches 87% top-1 accuracy against 77% for MiniLM,
and rejects 80% of out-of-scope questions against 60%. A reranker raised top-1 to 100% but cost 1.5–3.5 s per query on CPU, so it stays off.
Vector and full-text results are merged with RRF, and the `0.82` confidence threshold comes from the `calibrate` script.

**Cross-language answers.** The first eval showed that German and Dutch questions missed articles available only in English. The search tool now always queries in English,
and the 6 most common topics were translated into German and Dutch. Search accuracy rose from 88.3% to 100%, and answer accuracy from 80.0% to 91.7%.

**Fewer invented answers.** The model answers only from retrieved excerpts. Below the threshold the bot says it found nothing, and if the excerpts
do not contain the answer, the model calls `report_no_answer`. Only sources the answer actually cites are shown to the customer.

**Critical logic lives in code.** Email verification, the lockout after 5 failed lookups, escalation triggers and handover messages do not depend on the model.
The model passes only a reason to `create_ticket`, while the system builds the summary and history.

**Issues found only with the real model.** It showed customers raw statuses such as `OUT_FOR_DELIVERY` and called 21 October "tomorrow" on 21 October.
It now receives plain-language statuses, the current date in the shop's time zone, and "today" and "tomorrow" already calculated by code.

**Cost control.** Tokens from every call are stored in `llm_usage` and converted to euros. An alert fires at 80% of the monthly budget, and the bot pauses at 100%.

## Getting Started

Only Docker with Compose v2 is required. Everything runs in containers.

```bash
cp .env.example .env   # uses the fake model by default: offline and free
make up                # the first build downloads about 2.3 GB of models and takes around 10 minutes
```

| URL | Description |
| --- | --- |
| http://localhost:8088/demo | demo page with the widget |
| http://localhost:8088/admin/ | admin panel (username `admin`, password from `ADMIN_PASSWORD`) |
| http://localhost:8088/docs | Swagger (outside production) |
| http://localhost:8088/metrics | Prometheus metrics |

To use a real model, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` in `.env`, then run `docker compose up -d api worker`.
Providers can be switched without code changes: `anthropic`, `openai`, `bedrock` or `fake`. All settings are described in [.env.example](.env.example).

Things to ask the bot: `How long do I have to return an item?`, `Waar is mijn bestelling 1006? mark.visser@example.nl`,
`Do you have an Alpine Shell Jacket in black, size XL?`, `Ich möchte mit einem Mitarbeiter sprechen`.
Test orders are listed in [shopify_data.py](backend/mock_services/shopify_data.py).

## Tests and Measurements

```bash
make test        # ruff + pytest, 227 tests
make eval        # free search eval: 60 in-scope and 10 out-of-scope questions, report in ./reports/
make loadtest    # 30 concurrent conversations on 4 CPUs
make monitoring  # Prometheus (:9091) and Grafana (:3001)
make lab         # compare embedding models
make calibrate   # tune the threshold after changing the model
```

The `tests`, `eval` and `api-load` services always use the fake model with an empty key, so they cannot spend money by accident.
The real model is used only by the `api` service with settings from `.env`, and by the `eval-real` profile.

## Deployment

**AWS:** Terraform and a single EC2 instance running the same Compose stack, with secrets in SSM, backups in S3, logs and alarms in CloudWatch, and optional Claude through Bedrock.
See [infra/aws/README.md](infra/aws/README.md). The code passes `terraform validate` and `shellcheck` but has **not been applied to a real AWS account**.

**Any VPS:**

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

## Project Structure

```
backend/app/            agent/ (graph, tools, prompts, safeguards), rag/ (loading, chunking, indexing, search),
                        integrations/, services/, api/, workers/, static/ (widget and admin panel)
backend/mock_services/  Shopify GraphQL and Freshdesk mocks
backend/seed/           demo knowledge base and its generator
backend/eval/           questions and eval runner
backend/scripts/        data loading, calibration, load test, graph tracing
backend/tests/          pytest suite
infra/                  Prometheus, Grafana, Caddy, AWS (Terraform and scripts)
```

## Limitations

- Out-of-scope handling is below its target of 10 out of 10.
- Most help center articles exist only in English, so German and Dutch answers can rely on them.
- Load behavior with the real model has not been measured.
- The Shopify and Freshdesk clients have been tested against the mocks only.
- Product availability updates once a day.
- If Freshdesk is unavailable, the customer receives `SUPPORT_EMAIL`, and the ticket is not retried.
- Changing to an embedding model with a different vector size requires a migration and a full reindex.
- It runs on a single server without high availability.
