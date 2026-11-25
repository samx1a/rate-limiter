# Shortcuts for the commands I kept typing. Run `make` to list them.
.DEFAULT_GOAL := help
REDIS_PORT ?= 6399
TEST_REDIS := redis://127.0.0.1:$(REDIS_PORT)/0

.PHONY: help install dev check lint test test-redis redis cov load docker up

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-11s %s\n", $$1, $$2}'

install: ## install the app and dev tools
	uv sync --extra dev

dev: ## run the app with reload on :8000 (in-memory)
	uv run uvicorn webapp.main:app --reload

lint: ## format check, lint, types
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy

test: ## tests; Redis ones skip if no Redis
	uv run pytest

redis: ## start a throwaway Redis on REDIS_PORT (6399) just for tests
	redis-server --port $(REDIS_PORT) --save "" --appendonly no --daemonize yes

test-redis: ## every test against the throwaway Redis
	REDIS_URL=$(TEST_REDIS) REQUIRE_REDIS=1 REDIS_DISPOSABLE=1 uv run pytest

cov: ## tests with a coverage report
	REDIS_URL=$(TEST_REDIS) REDIS_DISPOSABLE=1 uv run pytest --cov

check: lint test-redis ## what CI runs

load: ## 20s load test against a server on :8000
	uv run python -m loadtest.loadtest --url http://127.0.0.1:8000/api/hello \
	  --duration 20 --concurrency 50 --users 100 --limit 10/10s

docker: ## build the production image
	docker build -t rate-limiter .

up: ## two app servers + Redis (see compose.yaml)
	docker compose up --build
