# Agent Spend Control — Build Plan

A spend-control layer that sits between an AI agent and a real payment rail. The agent never holds a real card or API key. Every purchase goes through a policy engine and a durable workflow: budget reservation, deterministic policy gates, an LLM intent check, human step-up approval when needed, and a single-use, merchant-locked virtual card with a hard limit. Money correctness is guaranteed under concurrency and failure, and proven by an invariant test suite.

**Read this whole file before writing any code. Work one phase at a time. Do not start the next phase until the current phase's acceptance criteria pass.**

---

## 1. Design principles (non-negotiable)

1. **The agent is untrusted.** Never trust what the agent says about a merchant, an amount, or a cart. Verify facts from authoritative sources: the cart from the checkout or merchant data, and the merchant and amount from the card provider's authorization record.
2. **Deterministic for money and security; the LLM only for semantic judgment.** Hard gates (price, merchant, category, budget) are pass/fail checks in code. The LLM judge can approve only *within* bounds the gates have already enforced. It can never override them.
3. **Fail closed.** Timeouts, ambiguity, and errors all lead to *no purchase*. Autonomy is something the user grants narrowly, never the default.
4. **Revoke capability before releasing funds.** On cancel or expiry: close the card first, confirm it's closed, then release the reservation.
5. **Every activity is idempotent.** Temporal runs activities at least once. Running any activity twice must have the same effect as running it once.
6. **Workflows are deterministic.** No I/O, randomness, or `datetime.now()` inside workflow code. Use `workflow.now()`. All side effects go in activities.
7. **Never log, store, or print full card numbers (PAN), CVVs, or expiries.** Treat them as secrets. Pass them to the agent only at the moment of checkout.
8. **The real payment provider stays off by default.** Real Privacy API calls only happen when `PRIVACY_LIVE=true`. All tests use the mock provider.

---

## 2. Tech stack

- **Python 3.12**, `uv` for dependency management
- **Temporal** Python SDK (`temporalio`); local dev server via `temporal server start-dev`
- **Postgres 16** (docker-compose); `asyncpg` or SQLAlchemy 2.0 async; migrations via Alembic
- **FastAPI** for the API: purchase requests, approval endpoint, mandate creation
- **pytest** + `pytest-asyncio`; Temporal's time-skipping test environment
- **LLM provider:** pluggable interface; default implementation uses Fireworks (OpenAI-compatible API)
- **Card provider:** pluggable `CardProvider` interface with `MockCardProvider` (fault injection) and `PrivacyCardProvider` (real)
- **Observability:** structured JSON logs; OpenTelemetry tracing across API → workflow → activities

---

## 3. Repo structure

```
agent-spend-control/
├── PLAN.md
├── README.md
├── docker-compose.yml          # postgres (+ optional temporal)
├── pyproject.toml
├── migrations/                 # alembic
├── src/spendctl/
│   ├── api/                    # FastAPI app: purchases, approvals, mandates
│   ├── db/                     # models, repositories, transactions
│   ├── ledger/                 # double-entry ledger, reconciliation
│   ├── providers/
│   │   ├── base.py             # CardProvider interface
│   │   ├── mock.py             # MockCardProvider w/ fault injection
│   │   └── privacy.py          # PrivacyCardProvider (live, flag-gated)
│   ├── policy/
│   │   ├── gates.py            # deterministic hard gates
│   │   ├── cart_binding.py     # identity hash + price tolerance
│   │   ├── risk.py             # velocity/structuring/novelty scoring
│   │   └── decision.py         # decision table
│   ├── llm/
│   │   ├── extraction.py       # NL request → structured mandate
│   │   └── judge.py            # intent-match judge
│   ├── workflows/
│   │   ├── purchase.py         # PurchaseWorkflow
│   │   └── activities.py       # all activities (idempotent)
│   ├── sweeper.py              # orphan card reconciliation job
│   └── worker.py               # Temporal worker entrypoint
├── tests/
│   ├── invariants.py           # invariant assertion functions
│   ├── unit/
│   ├── workflow/               # time-skipping workflow tests
│   ├── fault_injection/
│   ├── concurrency/
│   └── replay/                 # saved histories + Replayer tests
└── evals/
    ├── judge_dataset.jsonl     # labeled carts incl. adversarial
    ├── run_judge_eval.py       # precision/recall/calibration
    └── adversarial/            # prompt-injection & structuring scenarios
```

---

## 4. Data model (Postgres)

- **`mandates`**: id, user_id, raw_request (text), structured (jsonb: category, brands, max_price, allowed_merchants, condition, quantity), budget_id, confirmed_at (null until the user confirms), expires_at, status
- **`budgets`**: id, user_id, limit_amount, spent, reserved, window_type (`task` | `rolling`), window_seconds, created_at. **CHECK: `spent + reserved <= limit_amount`**
- **`reservations`**: id, **purchase_id (UNIQUE)**, budget_id, amount, status (`held` | `captured` | `released`), expires_at
- **`purchases`**: id (= purchase_id), mandate_id, workflow_id, state, merchant, cart_identity_hash, approved_total, final_amount, created_at, updated_at
  - States: `requested → reserved → pending_approval → approved → card_issued → authorized → settled` | `rejected` | `expired` | `refunded` | `partially_refunded`
  - Every transition is validated against an allowed-transitions map and written to `purchase_events`
- **`purchase_events`**: append-only audit log (purchase_id, from_state, to_state, reason, actor, at)
- **`cards`**: id, purchase_id, provider_card_token, **idem_key (UNIQUE)**, merchant_lock, limit_amount, status (`open` | `paused` | `closed`). **No PAN column.**
- **`ledger_entries`**: double-entry; id, txn_id, account (`user_funds`, `reserved`, `merchant_payable`, ...), debit, credit, purchase_id, at. Invariant: for each txn_id, sum(debit) = sum(credit)
- **`approvals`**: id, purchase_id, cart_snapshot (jsonb), cart_identity_hash, requested_at, deadline, decision, decided_at

---

## 5. Phases

### Phase 0: Scaffolding (½ day)
- uv project, docker-compose Postgres, Alembic set up, Temporal dev server instructions in the README
- FastAPI health endpoint; Temporal worker that starts with no workflows
- `make test`, `make up`, `make worker` targets
- **Done when:** `make up && make test` passes on a clean checkout.

### Phase 1: Budgets & idempotent reservations (2 days)
- Implement `reserve(purchase_id, budget_id, amount)` as **one DB transaction**:
  1. `SELECT ... FOR UPDATE` on the budget row
  2. If a reservation with this `purchase_id` already exists → return it unchanged (idempotent)
  3. If `limit - spent - reserved < amount` → return `insufficient`
  4. Insert the reservation (`held`, with `expires_at`), increment `budgets.reserved`, commit
- `release(purchase_id)` and `capture(purchase_id, final_amount)`, both idempotent, guarded by reservation status
- Tests:
  - **Concurrency:** 20 parallel `reserve` calls of $25 against a $200 budget → exactly 8 succeed; `reserved == 200`
  - **Idempotency:** calling `reserve` twice with the same purchase_id → one reservation; budget incremented once
  - Double `release` → funds released once
- **Done when:** concurrency and idempotency tests pass 50 runs in a row (`pytest --count=50` or a loop).

### Phase 2: Card provider interface + mock with fault injection (2 days)
- `CardProvider` interface: `create_card(idem_key, merchant_lock, limit)`, `find_card_by_key(idem_key)`, `close_card(token)`, `pause_card(token)`, `list_cards()`, `list_transactions(since)`
- `MockCardProvider`:
  - In-memory store; enforces merchant lock and limit on simulated charges
  - **Fault injection:** configurable failures by method and call number: raise before the side effect; raise *after* the side effect (the card is created but the caller sees an error); hang past the timeout
  - Simulated authorization/settlement events, including settlement amounts different from the authorization, and late or out-of-order events
- `create_card` activity pattern: derive `idem_key = f"{workflow_id}-card"` → `find_card_by_key` → if found and the params match, adopt it; if found and the params differ, raise a non-retryable error; else create
- `sweeper.py`: lists provider cards, closes any card with no matching active purchase in the DB, and logs each closure
- **Done when:** a test where `create_card` succeeds at the provider but errors to the caller, then retries, results in exactly one card; the sweeper test closes an injected orphan.

---

## 6. Working rules for Claude Code

- Follow the phases in order. At the end of each phase, run the full test suite and report results before moving on.
- Write the invariant and concurrency tests **before or alongside** the implementation they cover, not after.
- When unsure whether an operation is idempotent, ask: "If this runs twice, what changes?" If anything changes, fix it before continuing.
- Keep workflow code free of I/O. If you need data inside a workflow, fetch it in an activity.
- If you change workflow code in a way that changes its decisions after histories already exist, use Temporal's patching (`workflow.patched`) and add a replay test.
- Never commit secrets. Use `.env` (gitignored) for `PRIVACY_API_KEY` and `FIREWORKS_API_KEY`.
- Prefer small, reviewable commits, one per meaningful step.
