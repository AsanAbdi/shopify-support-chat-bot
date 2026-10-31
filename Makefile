# Everything runs in Docker. `make help` lists targets.
.DEFAULT_GOAL := help
COMPOSE ?= docker compose

help: ## show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

up: ## build and start the stack (api on http://localhost:8088)
	$(COMPOSE) up -d --build
seed: ## load demo knowledge base + sync the catalog from mock Shopify
	$(COMPOSE) run --rm api python -m scripts.seed_demo
down: ## stop the stack (keeps data)
	$(COMPOSE) down
reset: ## stop the stack and DELETE all data volumes
	$(COMPOSE) down -v
logs: ## follow logs
	$(COMPOSE) logs -f --tail=100 api worker
test: ## lint + tests inside Docker
	$(COMPOSE) --profile test run --rm --build tests
eval: ## run the 60+10 question eval, report in ./reports
	$(COMPOSE) --profile eval run --rm eval
loadtest: ## 30 concurrent dialogs against a rate-limit-free API instance (needs `make up` data)
	$(COMPOSE) --profile load run --rm loadtest
	$(COMPOSE) --profile load rm -sf api-load
monitoring: ## start Prometheus + Grafana (localhost:9091 / :3001)
	$(COMPOSE) --profile monitoring up -d
migration: ## autogenerate an Alembic migration: make migration m="message"
	$(COMPOSE) run --rm -v $(CURDIR)/backend/migrations:/app/migrations migrate alembic revision --autogenerate -m "$(m)"
lab: ## compare embedding models on the demo KB (LAB_MODELS=minilm,e5-large LAB_RERANK=1)
	$(COMPOSE) --profile lab run --rm lab
calibrate: ## recalibrate the "nothing found" threshold after changing the embedding model
	$(COMPOSE) run --rm --no-deps -e LLM_PROVIDER=fake -e ANTHROPIC_API_KEY= api python -m scripts.calibrate
kb: ## regenerate the demo knowledge base (seed/kb) and the eval questions
	$(COMPOSE) run --rm --no-deps -u root -v $(CURDIR)/backend:/app api sh -c "python seed/build_kb.py && python eval/build_questions.py"
prod-rehearsal: ## run the production overlay on this machine: https://localhost:8443 (self-signed)
	DOMAIN=localhost ACME_EMAIL=you@example.com POSTGRES_PASSWORD=rehearsal COMPOSE_PROFILES=mock,demo $(COMPOSE) -p shopprod -f docker-compose.yml -f docker-compose.prod.yml -f infra/prodtest.yml up -d --build
aws-validate: ## validate the Terraform code and lint the AWS scripts (Docker only)
	docker run --rm --entrypoint sh -v $(CURDIR)/infra/aws:/aws -w /aws/terraform hashicorp/terraform:1.9 -c "terraform init -backend=false -input=false >/dev/null && terraform validate && terraform fmt -check -recursive"
	docker run --rm -v $(CURDIR)/infra/aws/scripts:/s -w /s koalaman/shellcheck:stable -x -S style bootstrap.sh deploy.sh backup.sh restore.sh put-secrets.sh
