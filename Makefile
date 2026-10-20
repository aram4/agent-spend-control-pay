.PHONY: up down migrate test worker api

# Uses docker compose when available; otherwise provisions a local Postgres.
up:
	@if command -v docker >/dev/null 2>&1; then \
		docker compose up -d --wait postgres; \
	else \
		echo "docker not found; using local Postgres"; \
		./scripts/local-db.sh; \
	fi
	uv sync
	$(MAKE) migrate

down:
	docker compose down

migrate:
	uv run alembic upgrade head

test:
	uv run pytest

worker:
	uv run python -m spendctl.worker

api:
	uv run uvicorn spendctl.api.app:app --reload
