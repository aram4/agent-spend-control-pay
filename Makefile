.PHONY: up down migrate test stress worker api

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

stress:
	uv run pytest tests/concurrency tests/unit/test_reservations.py --count=50 -x -q

worker:
	uv run python -m spendctl.worker

api:
	uv run uvicorn spendctl.api.app:app --reload
