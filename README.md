# Agent Spend Control

A spend-control layer between an AI agent and a real payment rail. The agent never holds a real card: every purchase goes through budget reservation, deterministic policy gates, an LLM intent check, optional human approval, and a single-use, merchant-locked virtual card. See [PLAN.md](PLAN.md) for the full design and build phases.

## Status

Working today: an end-to-end `PurchaseWorkflow` on Temporal against a mock card provider:

1. **Reserve** the amount against the budget (row-locked, idempotent), or reject if it doesn't fit
2. **Issue** a single-use virtual card locked to one merchant, with the reservation as its hard limit
3. **Wait** for the merchant's charge, read from the card provider's records, never from the agent
4. **Close** the card as soon as it's charged or the checkout window ends
5. **Capture** what actually settled and free the rest of the hold, or release everything if nothing was charged

Still to come: deterministic policy gates, the LLM intent check, human approval, and the real (Privacy.com) card provider. See [PLAN.md](PLAN.md).

## Demo

```sh
temporal server start-dev   # terminal 1 (web UI on http://localhost:8233)
make demo                   # terminal 2
```

The demo runs four purchases against a $100 budget and prints a link to each one's history in the Temporal UI:

| Scenario | Outcome |
|---|---|
| Normal purchase; the merchant settles for less than it authorized | Settled; only the settled amount is spent |
| Purchase larger than the remaining budget | Rejected before any card exists |
| Card charged by the wrong merchant, then abandoned | Declined at the card; card closed and funds released when checkout times out |
| Provider creates the card but the response is lost | Temporal retries; the retry adopts the existing card, so no duplicate |

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
| `make stress` | Concurrency + idempotency tests for the money path, 50 runs in a row |
| `make demo` | Run the end-to-end purchase demo (needs `temporal server start-dev`) |
| `make worker` | Run the Temporal worker |
| `make api` | Run the FastAPI app with reload |
| `make migrate` | Apply Alembic migrations |
| `make down` | Stop docker compose services |

## Safety defaults

- `PRIVACY_LIVE=false` by default: no real card-provider calls unless explicitly enabled.
- Secrets (`PRIVACY_API_KEY`, `FIREWORKS_API_KEY`) live in `.env`, which is gitignored.
