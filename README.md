# Agent Spend Control

A spend-control layer between an AI agent and a real payment rail. The agent never holds a real card: every purchase goes through budget reservation, deterministic policy gates, an LLM intent check, optional human approval, and a single-use, merchant-locked virtual card. See [PLAN.md](PLAN.md) for the full design and build phases.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 automatically)
- Postgres 16, via **either** Docker (`docker compose`; on macOS without Docker Desktop: `brew install docker docker-compose colima && colima start`) **or** a local install (e.g. `brew install postgresql@16 && brew services start postgresql@16`)
- Temporal CLI, for running the worker against a dev server (tests don't need it)

## Quick start

```sh
cp .env.example .env   # optional; defaults work for local dev
make up                # start/provision Postgres, install deps, run migrations
make test              # run the test suite
```

`make up` uses `docker compose` when Docker is installed. Otherwise it runs `scripts/local-db.sh`, which creates the `spendctl` role and the `spendctl` / `spendctl_test` databases on the Postgres already listening on `localhost:5432`; point `DATABASE_URL` and `TEST_DATABASE_URL` in `.env` at port 5432 in that case.

The Docker Postgres listens on host port **5433** so it can coexist with a local Postgres on 5432.

## Temporal dev server

Install the CLI and start a local server (gRPC on `:7233`, web UI on http://localhost:8233):

```sh
brew install temporal
temporal server start-dev
```

Or with Docker: `docker compose --profile temporal up -d`.

Then, in separate terminals:

```sh
make worker   # Temporal worker on task queue "spendctl"
make api      # FastAPI on http://localhost:8000 (GET /health, GET /ready)
```

Tests use Temporal's time-skipping test environment, which the SDK downloads on first run, so no dev server is needed for `make test`.

## Make targets

| Target | What it does |
|---|---|
| `make up` | Postgres up (Docker or local), `uv sync`, `alembic upgrade head` |
| `make test` | `pytest` against the `spendctl_test` database |
| `make worker` | Run the Temporal worker |
| `make api` | Run the FastAPI app with reload |
| `make migrate` | Apply Alembic migrations |
| `make down` | Stop docker compose services |

## Safety defaults

- `PRIVACY_LIVE=false` by default: no real card-provider calls unless explicitly enabled.
- Secrets (`PRIVACY_API_KEY`, `FIREWORKS_API_KEY`) live in `.env`, which is gitignored.
