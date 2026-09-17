# AgentCMS — one-command developer loop (#2).
#
#   make dev     start Postgres + MinIO, migrate, serve the API on :8000
#   make test    ephemeral-Postgres test suite (no shared state between runs)
#   make lint    ruff + mypy
#
# Nothing here needs a global Python: a project-local .venv is created on demand.

SHELL      := /bin/bash
VENV       := .venv
PY         := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
UVICORN    := $(VENV)/bin/uvicorn
PYTEST     := $(VENV)/bin/pytest
ALEMBIC    := $(VENV)/bin/alembic
RUFF       := $(VENV)/bin/ruff
MYPY       := $(VENV)/bin/mypy
HOST       ?= $(shell grep -E '^HOST=' .env 2>/dev/null | cut -d= -f2 || echo 127.0.0.1)
PORT       ?= $(shell grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8000)
HOST       := $(if $(HOST),$(HOST),127.0.0.1)
PORT       := $(if $(PORT),$(PORT),8000)
COMPOSE    := docker compose
DOCS_URL   := http://$(HOST):$(PORT)/docs

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/python:
	@echo "==> creating virtualenv in $(VENV)"
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

.PHONY: venv
venv: $(VENV)/bin/python ## Create the virtualenv

.PHONY: install
install: venv ## Install runtime + dev dependencies into .venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e ".[dev]"
	@test -f .env || (cp .env.example .env && echo "==> wrote .env from .env.example")

.PHONY: up
up: ## Start Postgres in the background
	$(COMPOSE) up -d db
	@$(MAKE) --no-print-directory wait-for-db

.PHONY: up-storage
up-storage: ## Start Postgres + MinIO (S3-compatible storage) in the background
	$(COMPOSE) --profile storage up -d db minio
	@$(MAKE) --no-print-directory wait-for-db

.PHONY: down
down: ## Stop the local stack (keeps volumes)
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop the local stack and delete its volumes
	$(COMPOSE) down -v

.PHONY: wait-for-db
wait-for-db: ## Block until Postgres accepts connections
	@for i in $$(seq 1 60); do \
		if $(COMPOSE) exec -T db pg_isready -q -U $${POSTGRES_USER:-agentcms} 2>/dev/null; then \
			echo "==> postgres is ready"; exit 0; \
		fi; \
		sleep 1; \
	done; \
	echo "postgres did not become ready in 60s" >&2; exit 1

.PHONY: migrate
migrate: install ## Apply Alembic migrations (upgrade head)
	$(ALEMBIC) upgrade head

.PHONY: migration
migration: install ## Autogenerate a migration: make migration m="add foo"
	@test -n "$(m)" || (echo 'usage: make migration m="message"' >&2; exit 2)
	$(ALEMBIC) revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: install ## Roll the schema back one revision
	$(ALEMBIC) downgrade -1

.PHONY: seed
seed: install ## Create the demo site + 3 posts + 1 token
	$(PY) -m scripts.seed

.PHONY: dev
dev: install up migrate ## Start Postgres, migrate, and serve the API with reload
	@echo ""
	@echo "  AgentCMS is starting:"
	@echo "    API         $(DOCS_URL)"
	@echo "    OpenAPI     http://$(HOST):$(PORT)/openapi.json"
	@echo "    health      http://$(HOST):$(PORT)/healthz   (liveness, no DB)"
	@echo "    readiness   http://$(HOST):$(PORT)/readyz    (DB reachable)"
	@echo ""
	$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload

.PHONY: serve
serve: install ## Serve the API without docker/reload (bring your own Postgres)
	$(UVICORN) app.main:app --host $(HOST) --port $(PORT)

.PHONY: test
test: install ## Run the test suite against an ephemeral Postgres
	$(PYTEST)

.PHONY: lint
lint: install ## ruff + mypy (both must be clean)
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY)
	@echo "==> lint clean"

.PHONY: fmt
fmt: install ## Auto-format and auto-fix
	$(RUFF) format .
	$(RUFF) check --fix .

.PHONY: gate
gate: lint test ## CI gate: the lint + test jobs of .github/workflows/ci.yml

.PHONY: gate-migrations
gate-migrations: install ## CI gate: the migrations job (DESTRUCTIVE: wipes every table in $$DATABASE_URL)
	$(ALEMBIC) upgrade head
	$(ALEMBIC) downgrade base
	$(ALEMBIC) upgrade head
	$(ALEMBIC) check

.PHONY: check
check: gate ## Alias for gate

.PHONY: openapi
openapi: install ## Dump the OpenAPI 3.1 document to openapi.json
	$(PY) -c "import json;from app.main import app;print(json.dumps(app.openapi(),indent=2,sort_keys=True))" > openapi.json
	@echo "==> openapi.json written"

.PHONY: docker-build
docker-build: ## Build the production image
	docker build -t agentcms:local .

.PHONY: prod-up
prod-up: ## Serve API + DB with compose.prod.yml
	$(COMPOSE) -f compose.prod.yml up --build -d
	@echo "==> prod stack up; logs: docker compose -f compose.prod.yml logs -f api"

.PHONY: prod-down
prod-down: ## Tear down compose.prod.yml
	$(COMPOSE) -f compose.prod.yml down

.PHONY: clean
clean: ## Remove caches and the local venv
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
