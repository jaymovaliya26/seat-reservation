.DEFAULT_GOAL := help
.PHONY: help install db run test lint fmt typecheck check down

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install dependencies into .venv
	uv sync

db: ## Start Postgres only (host port 5433)
	docker compose up -d --wait db

run: db ## Run the API locally with auto-reload on :8000
	uv run uvicorn app.main:create_app --factory --reload --port 8000

test: db ## Run the test suite against the compose Postgres
	uv run pytest

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Format and auto-fix
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Static type check
	uv run mypy app

check: lint typecheck test ## Everything CI runs

down: ## Stop containers
	docker compose down
