.DEFAULT_GOAL := help
PY ?= python
COMPOSE = docker compose -f deploy/docker-compose.yml

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Create .venv and install everything (app + dev tools)
	$(PY) -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -e ".[dev]"
	@test -f .env || cp .env.example .env

up: ## Start Postgres + Redis only (for running the API from VS Code)
	$(COMPOSE) up -d postgres redis

stack: ## Start the whole stack in Docker (API, MockPay, workers, Prometheus, Grafana)
	$(COMPOSE) up --build

down: ## Stop everything
	$(COMPOSE) down

nuke: ## Stop everything AND delete the database volume
	$(COMPOSE) down -v

migrate: ## Apply DB migrations
	alembic upgrade head

migration: ## Create a migration from model changes: make migration m="add foo column"
	alembic revision --autogenerate -m "$(m)"

api: ## Run the API with auto-reload
	uvicorn app.main:app --reload --port 8000

mockpay: ## Run MockPay
	uvicorn mockpay.app:app --reload --port 8100

worker: ## Run a worker: make worker w=charge|inbox|sweeper|backfill
	$(PY) -m app.workers.run $(w)

test: ## Unit + integration tests
	pytest tests/unit tests/integration -q

unit: ## Unit tests only (no Docker needed)
	pytest tests/unit -q

chaos: ## Failure-injection tests: duplicates, webhook loss, out-of-order delivery, crashes
	pytest tests/chaos -v

cov: ## All tests with a coverage report
	pytest --cov=app --cov-report=term-missing --cov-report=html

lint: ## Lint + format check + type check
	ruff check . && ruff format --check . && mypy app

fmt: ## Auto-format
	ruff check . --fix && ruff format .

loadtest: ## Locust against a running API (web UI on :8089)
	locust -f loadtest/locustfile.py --host http://localhost:8000

demo: ## Walk through every failure scenario against the running stack
	bash scripts/demo.sh

.PHONY: help install up stack down nuke migrate migration api mockpay worker test unit chaos cov lint fmt loadtest demo
