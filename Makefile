.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------- setup
.PHONY: install
install: ## Install the package with dev extras
	pip install -e ".[dev]"

.PHONY: install-all
install-all: ## Install with docling + presidio extras too
	pip install -e ".[dev,docling,pii]"

.PHONY: env
env: ## Create .env from the example
	@test -f .env || cp .env.example .env && echo ".env ready"

# --------------------------------------------------------------- lifecycle
.PHONY: up
up: env ## Start the full stack
	$(COMPOSE) up -d --build
	@echo "API      http://localhost:8000/docs"
	@echo "Grafana  http://localhost:3000 (admin/admin)"
	@echo "Jaeger   http://localhost:16686"
	@echo "MinIO    http://localhost:9001 (minioadmin/minioadmin)"

.PHONY: down
down: ## Stop the stack
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stack and delete all volumes
	$(COMPOSE) down -v --remove-orphans

.PHONY: logs
logs: ## Tail API + worker logs
	$(COMPOSE) logs -f api worker

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps

# ---------------------------------------------------------------- database
.PHONY: migrate
migrate: ## Apply migrations
	alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add x"
	alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	alembic downgrade -1

# ------------------------------------------------------------------- local
.PHONY: dev
dev: ## Run the API with reload
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: worker
worker: ## Run the ingestion worker
	arq app.workers.worker.WorkerSettings

.PHONY: mcp
mcp: ## Run all three MCP servers locally
	@python -m app.mcp.servers.accounts_server & \
	 python -m app.mcp.servers.transactions_server & \
	 python -m app.mcp.servers.service_server & \
	 wait

.PHONY: seed
seed: ## Load sample knowledge-base documents
	python scripts/seed_knowledge_base.py

.PHONY: smoke
smoke: ## End-to-end smoke test against a running stack
	python scripts/smoke_test.py

# ------------------------------------------------------------------ quality
.PHONY: test
test: ## Run unit tests
	pytest tests/unit -v

.PHONY: test-integration
test-integration: ## Run integration tests (needs the stack up)
	pytest tests/integration -v -m integration

.PHONY: cov
cov: ## Tests with coverage
	pytest --cov=app --cov-report=term-missing --cov-report=html

.PHONY: lint
lint: ## Lint
	ruff check app tests
	ruff format --check app tests

.PHONY: fmt
fmt: ## Format and autofix
	ruff format app tests
	ruff check --fix app tests

.PHONY: types
types: ## Type check
	mypy app

.PHONY: check
check: lint types test ## Everything CI runs

# --------------------------------------------------------------------- ops
.PHONY: eval
eval: ## Run the offline evaluation suite
	python scripts/run_eval.py

.PHONY: index
index: ## Create/verify the OpenSearch index
	python scripts/bootstrap_index.py

.PHONY: token
token: ## Print a local dev bearer token
	@python scripts/make_token.py
