# AI-ассистент поддержки для интернет-магазина (RAG + агент)

FastAPI + LangChain/LangGraph, PostgreSQL + pgvector, Celery + Redis. Чат-виджет вставляется одним `<script>`,
админка и API идут из того же сервиса. **Локально всё запускается только в Docker**, на сервере (AWS) это тот же compose-стек.

Что умеет: ответы по базе знаний со ссылками на источники (DE/EN/NL, стриминг), статус заказа по номеру и email,
поиск товаров и наличия по размеру и цвету, передача оператору в Freshdesk с summary и историей, админка со статистикой и расходом на LLM.
Развёртывание на AWS: [infra/aws/README.md](infra/aws/README.md).

> **Статус.** Это проект «как для реального бизнеса», но реального клиента нет. Shopify и Freshdesk **замоканы** контейнером `mock-services`
> (Shopify повторяет Admin GraphQL API по документации: стоимость запросов, троттлинг, ошибки доступа, окно заказов 60 дней).
> Данные синтетические: вымышленный магазин Nordvik Outdoor, ~1500 SKU, 89 документов базы знаний, 20 заказов во всех состояниях.

## Быстрый старт

Нужен только Docker (Compose v2).

```bash
cp .env.example .env          # значения по умолчанию рабочие (LLM по умолчанию fake: офлайн, бесплатно)
make up                       # = docker compose up -d --build. Первая сборка скачивает модели (~2,3 ГБ) и идёт ~10 минут
# демо-данные (89 документов + каталог ~1500 SKU из mock Shopify) загружает одноразовый сервис `seed`; повторно: make seed
```

| URL | Что там |
| --- | --- |
| http://localhost:8088/demo | демо-страница с виджетом |
| http://localhost:8088/admin/ | админка (логин `admin`, пароль из `ADMIN_PASSWORD`) |
| http://localhost:8088/docs | Swagger (только не в prod) |
| http://localhost:8088/metrics | метрики Prometheus (в prod закрыто Caddy) |

Попробуйте в виджете: `How long do I have to return an item?`, `Waar is mijn bestelling 1006? mark.visser@example.nl`,
`Do you have an Alpine Shell Jacket in black, size XL?`, `Ich möchte mit einem Mitarbeiter sprechen`.
Тестовые заказы мока (номер → email): 1001 `anna.schmidt@example.de` (доставлен), 1002 `jan.devries@example.nl` (в пути), 1006 `mark.visser@example.nl` (курьер сегодня),
1007 `lena.fischer@example.de` (задержка), 1008 `tom.janssen@example.nl` (неудачная доставка), 1010 `peter.bakker@example.nl` (частично отправлен),
1011 `julia.hoffmann@example.de` (две посылки), 1013 `eva.dekker@example.nl` (возврат в процессе), 1004 `lukas.meier@example.de` (отменён). Полный список в `backend/mock_services/shopify_data.py`.

## Деньги: тесты и eval не тратят API-ключ

`docker compose` сервисы `tests`, `eval` и `api-load` принудительно работают на fake-LLM с пустым ключом: **случайно потратить деньги на них нельзя**.
Платный прогон только явно: `docker compose --profile eval-real run --rm eval-real` (на Haiku ~0,1–0,3 € за 70 вопросов). Работающий сервис `api` использует модель из `.env`.

## Тесты, линтер, eval, нагрузка (всё в Docker)

```bash
make test        # ruff + pytest: 220+ тестов (миграции, RAG, tools, эскалации, безопасность, API/WebSocket, GDPR, мок Shopify, Celery)
make eval        # бесплатный eval (fake LLM): 60 вопросов (20 DE/EN/NL) + 10 вопросов вне базы, отчёт в ./reports/
make loadtest    # 30 одновременных диалогов, 4 CPU как на VPS (fake LLM: меряется наш стек, не вендор)
make monitoring  # Prometheus (:9091) и Grafana (:3001, дашборд "Support assistant")
docker compose --profile lab run --rm lab                 # сравнение моделей эмбеддингов
docker compose run --rm --no-deps api python -m scripts.calibrate   # подбор порога «ничего не найдено» на живом пайплайне
```

**Что измерено** (на fake-LLM, см. «Деньги»):

| Показатель | Результат |
| --- | --- |
| Нужный документ в топ-5 поиска, 60 вопросов | 86,7% (EN 20/20, DE 16/20, NL 16/20); цель 85% относится к ответам реальной модели и пока не измерена |
| Вопросы вне базы | заведомо посторонние (4 из 10) отсекаются порогом; «пограничные» отдаются LLM, которая обязана честно отказаться |
| Нагрузка 30 диалогов, 4 CPU | ошибок 0, первый токен p50 0,2–0,3 с, p95 0,9–1,4 с (цель 2,5 с); к этому добавится задержка модели |

Как устроен выбор модели эмбеддингов (измерено на этих данных): `multilingual-e5-large` даёт top-1 87% против 77% у MiniLM при 90 мс на запрос на одном потоке CPU.
Кросс-энкодер-реранкер даёт top-1 100%, но стоит 1,5–3,5 с на запрос на CPU, поэтому по умолчанию выключен (`RERANKER=none`).
Порог `RETRIEVAL_MIN_SIMILARITY=0.82` специфичен для модели: после её смены запустите `scripts.calibrate`.

## Конфигурация (`.env`)

Полный список с комментариями в [.env.example](.env.example). Главное:

| Переменная | Назначение |
| --- | --- |
| `LLM_PROVIDER` / `LLM_MODEL` | `anthropic`, `openai`, `bedrock` (Claude внутри AWS) или `fake`. Меняется без правки кода |
| `ANTHROPIC_API_KEY` | ключ (в prod через SSM/секреты, не в репозитории) |
| `LLM_SUMMARY_MODEL` | опционально более дешёвая модель для summary |
| `LLM_PRICE_*_EUR_PER_MTOK`, `LLM_MONTHLY_BUDGET_EUR` | расчёт стоимости в € и месячный лимит: алерт при 80%, остановка бота при 100%. Для Haiku 4.5 выход стоит ~4,6 €/млн токенов |
| `EMBEDDINGS_*`, `RETRIEVAL_MIN_SIMILARITY` | модель эмбеддингов (локальная, ONNX, вшита в образ) и порог «ничего не найдено» |
| `SHOPIFY_*`, `FRESHDESK_*` | адреса и токены интеграций (по умолчанию mock) |
| `JWT_SECRET`, `ADMIN_PASSWORD` | в `APP_ENV=prod` сервис не стартует с дефолтными значениями или с моками (кроме `ALLOW_MOCK_INTEGRATIONS=true` для демо) |
| `CORS_ORIGINS` | домены магазина, с которых разрешены виджет и WebSocket |
| `SENTRY_DSN`, `LANGFUSE_*` | трейсинг и ошибки (необязательно); email и телефоны маскируются перед отправкой |
| `ALERT_WEBHOOK_URL`, `ALERT_TELEGRAM_*` | куда слать алерты о бюджете LLM (Slack/Mattermost/Teams-совместимый webhook и/или Telegram) |

### Как сменить модель
```env
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5-5        # или claude-haiku-4-5-20251001
ANTHROPIC_API_KEY=sk-ant-...
# OpenAI: LLM_PROVIDER=openai, LLM_MODEL=gpt-..., OPENAI_API_KEY=...
# AWS:    LLM_PROVIDER=bedrock, LLM_MODEL=eu.anthropic.claude-haiku-4-5-20251001-v1:0, AWS_REGION=eu-central-1 (ключ не нужен, доступ по IAM-роли)
```
затем `docker compose up -d api worker`. После смены модели обновите цены `LLM_PRICE_*`.

### Как подключить реальные Shopify и Freshdesk
```env
SHOPIFY_BASE_URL=https://<store>.myshopify.com
SHOPIFY_ADMIN_TOKEN=shpat_...            # custom app: read_orders, read_products, read_inventory (+ read_all_orders для заказов старше 60 дней)
SHOPIFY_API_VERSION=2026-07              # меняйте раз в 6-9 месяцев
SHOPIFY_ORDER_NAME_FORMAT=#{number}      # если в магазине свой префикс/суффикс номеров заказов, например NV-{number}
FRESHDESK_BASE_URL=https://<domain>.freshdesk.com
FRESHDESK_API_KEY=...
```
Клиент написан под реальные правила API (проверено на моке, но не на живом Shopify): запросы укладываются в лимит 1000 пунктов, `THROTTLED` ждёт по `throttleStatus`,
`ACCESS_DENIED` (поле email требует одобренного доступа к защищённым данным клиентов) даёт понятную ошибку, товары с >30 вариантами добираются постранично.

## Как обновить базу знаний

Через админку (вкладка «Knowledge base», роль admin): загрузка PDF, DOCX, Markdown, CSV, HTML или добавление URL статьи Help Center.
Индексация идёт в фоне (Celery), статус виден в таблице. Повторная загрузка файла с тем же именем заменяет документ:
считается хэш контента, неизменённый документ пропускается, у изменённого пересчитываются эмбеддинги только изменившихся фрагментов.
URL-источники перечитываются раз в сутки (03:30), каталог Shopify синхронизируется раз в сутки (03:00); вручную: `POST /api/admin/catalog/sync`.
Демо-база генерируется скриптом `backend/seed/build_kb.py` (80 статей EN/DE/NL, 4 PDF, 4 CSV, 1 DOCX).

## Архитектура вкратце

```
widget.js ──WebSocket/SSE──► FastAPI ──► LangGraph: prepare → agent ⇄ tools → finalize
admin SPA ──REST+JWT───────►    │                      └→ escalate (тикет Freshdesk)
                                ├─ Postgres + pgvector: документы, чанки (hybrid: вектор + FTS, RRF), каталог, диалоги, чекпоинты графа
                                ├─ Redis: rate limit, блокировки диалога, брокер Celery
                                └─ Celery: индексация, синк каталога, retention 90 дней
```

Ключевые решения:
- **Быстрый путь для FAQ.** Перед вызовом модели граф ищет в базе; уверенное попадание идёт прямо в промпт, и вопрос стоит **один** вызов LLM вместо двух (задержка и деньги). Неуверенное: модель сама вызывает поиск (может перевести запрос на английский) или `report_no_answer`.
- **Авторизация в коде, а не в промпте.** `get_order_status` сверяет email в Python (константное время), ответ «нет заказа» и «неверный email» одинаковый, тестовые и гостевые заказы не отдаются, после 5 неудач в диалоге поиск блокируется. Инструменты только на чтение.
- **Эскалация детерминирована:** просьба человека, жалоба, спор о деньгах (DE/EN/NL; вопрос о правилах возврата остаётся вопросом, а «где мой возврат?» уходит оператору) и два промаха базы подряд. Если email клиента неизвестен, бот один раз просит его.
- **Против галлюцинаций:** ответ только по найденным фрагментам; порог схожести и инструмент `report_no_answer`; показываются только источники, на которые ответ сослался. Текст передачи оператору шаблонный.
- **Prompt injection:** эвристики на входе, данные пользователя и инструментов помечены как данные, в системный промпт встроен canary, стриминговый фильтр обрывает ответ при утечке.
- **GDPR:** модель эмбеддингов локальная, email и телефоны маскируются в логах, Sentry и Langfuse, диалоги и чекпоинты удаляются через 90 дней, удаление по запросу: `DELETE /api/admin/gdpr/customer?email=...`; бэкапы хранятся 35 дней.
- **Стоимость:** токены каждого вызова пишутся в `llm_usage` и переводятся в €; админка показывает расход за месяц и на диалог.
- **Передача оператору:** инструмент `create_ticket` получает от модели только причину; summary и историю система собирает сама, чтобы модель не могла их подменить.

## Деплой

**AWS (основной вариант):** Terraform + один EC2 с тем же compose-стеком, секреты в SSM, бэкапы в S3, логи и алармы в CloudWatch,
опционально Claude через Amazon Bedrock в ЕС. Пошаговое руководство, стоимость и ограничения: [infra/aws/README.md](infra/aws/README.md).
Код проверен `terraform validate`/`fmt` и `shellcheck`, но **ни разу не применялся к реальному аккаунту AWS**.

**Любой VPS:** `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` (Caddy выпускает HTTPS-сертификат, `db-backup` делает ежедневный `pg_dump`).
Репетиция прода на ноутбуке: `infra/prodtest.yml`.

Вставка на сайт Shopify (theme.liquid перед `</body>`):
```html
<script src="https://support.your-shop.com/widget.js" data-color="#0b6e4f" data-logo="https://.../logo.svg" data-title="Support" data-privacy-url="https://your-shop.com/privacy" defer></script>
```

## Структура репозитория

```
backend/app/        agent/ (граф, tools, промпты, guard, эскалация), rag/ (loaders, chunking, indexer, retriever),
                    integrations/ (shopify, freshdesk, catalog), services/ (chat, stats, budget, gdpr, alerts), api/, workers/, static/ (widget, admin)
backend/mock_services/  мок Shopify Admin GraphQL (graphql-core), Freshdesk v2, приёмники алертов
backend/tests/      pytest (+ tests/fixtures/kb: 11 небольших документов)
backend/eval/       набор вопросов и eval-скрипт;  backend/seed/  демо-база знаний и её генератор;  backend/scripts/  seed, calibrate, lab, loadtest
infra/              Prometheus, Grafana, Caddy, aws/ (Terraform + скрипты хоста);  docker-compose*.yml, .gitlab-ci.yml, Makefile
```

## Известные ограничения
- Качество ответов реальной модели (≥85%, 0 выдуманных фактов, задержка первого токена) **не измерено**: платный прогон `eval-real` ещё не запускался.
- Наличие товаров берётся из суточного синка; для актуальности в реальном времени нужен запрос в Shopify при ответе или чаще синк (для 1500 SKU можно перейти на Bulk Operations).
- Если Freshdesk недоступен в момент передачи, клиенту дают `SUPPORT_EMAIL`, а ошибка уходит в Sentry; автоповтор тикета не реализован.
- Размерность эмбеддингов (`EMBEDDINGS_DIM`) зашита в схему БД: при смене модели нужна миграция и полная переиндексация (миграция 0003 показывает как; затем `POST /api/admin/reindex-all` переиндексирует базу и каталог).
- Один EC2/VPS: без высокой доступности (путь к RDS/ECS описан в руководстве по AWS). WebSocket-сессии живут в памяти процесса `api`.
