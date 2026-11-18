"""End-to-end demo against a local Temporal dev server.

    temporal server start-dev     # in another terminal
    make demo

Runs a worker in-process with the mock card provider, so this script can play
the merchant: it charges the virtual card the workflow issues, the way a real
checkout would.
"""

import asyncio
import logging
import sys
import uuid
from decimal import Decimal

from temporalio.client import Client, WorkflowHandle

from spendctl.config import get_settings
from spendctl.db.budgets import create_budget, get_budget
from spendctl.providers.mock import FaultMode, MockCardProvider
from spendctl.worker import build_worker
from spendctl.workflows.purchase import PurchaseWorkflow
from spendctl.workflows.starter import start_purchase

UI = "http://localhost:8233/namespaces/{ns}/workflows/{wf}"
TIMING = dict(
    checkout_window_seconds=6,
    poll_interval_seconds=0.5,
    settlement_window_seconds=6,
    settlement_poll_interval_seconds=0.5,
)


def say(msg: str = "") -> None:
    print(msg, flush=True)


async def show_budget(budget_id: uuid.UUID) -> None:
    b = await get_budget(budget_id)
    say(f"   budget: limit ${b.limit_amount}  spent ${b.spent}  held ${b.reserved}  available ${b.available}")


async def wait_for_card(handle: WorkflowHandle) -> str | None:
    while True:
        status = await handle.query(PurchaseWorkflow.status)
        if status.card_token or status.state == "rejected":
            return status.card_token
        await asyncio.sleep(0.1)


async def finish(handle: WorkflowHandle, budget_id: uuid.UUID, ns: str) -> None:
    result = await handle.result()
    say(f"   result: {result.state.upper()}: {result.reason}")
    await show_budget(budget_id)
    say(f"   history: {UI.format(ns=ns, wf=handle.id)}")
    say()


async def main() -> None:
    # The worker logs a traceback for the injected failure in scenario 4; the demo narrates it instead.
    logging.getLogger("temporalio.activity").setLevel(logging.ERROR)
    settings = get_settings()
    try:
        client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    except Exception as e:
        sys.exit(f"Can't reach Temporal at {settings.temporal_address} ({e}).\n"
                 "Start it with: temporal server start-dev")

    provider = MockCardProvider()
    queue = f"spendctl-demo-{uuid.uuid4().hex[:8]}"
    ns = settings.temporal_namespace

    async def purchase(merchant: str, amount: str) -> WorkflowHandle:
        say(f"   agent requests ${amount} at {merchant}")
        return await start_purchase(
            client, budget_id=budget_id, merchant=merchant, amount=amount, task_queue=queue, **TIMING
        )

    async with build_worker(client, task_queue=queue, provider=provider):
        budget_id = await create_budget("demo-user", "100")
        say("Agent spend control: end-to-end demo")
        await show_budget(budget_id)
        say()

        say("1) Normal purchase; the merchant settles for less than authorized")
        h = await purchase("acme-books", "42.50")
        card = await wait_for_card(h)
        say(f"   card issued: {card} (single-use, locked to acme-books, limit $42.50)")
        txn = provider.authorize(card, "acme-books", "42.50")
        say(f"   merchant authorizes $42.50 -> {txn.status}")
        provider.settle(txn.token, "39.99")
        say("   merchant settles $39.99 (discount applied)")
        await finish(h, budget_id, ns)

        say("2) Purchase larger than the remaining budget")
        h = await purchase("acme-electronics", "80.00")
        await finish(h, budget_id, ns)

        say("3) Card used at the wrong merchant, then abandoned")
        h = await purchase("acme-books", "20.00")
        card = await wait_for_card(h)
        say(f"   card issued: {card} (locked to acme-books)")
        txn = provider.authorize(card, "sketchy-store", "20.00")
        say(f"   sketchy-store tries to charge $20.00 -> {txn.status} ({txn.decline_reason})")
        say(f"   no valid charge; waiting out the {TIMING['checkout_window_seconds']:g}s checkout window...")
        await finish(h, budget_id, ns)

        say("4) Provider creates the card but the response is lost")
        provider.inject("create_card", FaultMode.FAIL_AFTER)
        h = await purchase("acme-coffee", "15.00")
        card = await wait_for_card(h)
        cards = [c for c in await provider.list_cards() if c.idem_key == f"{h.id}-card"]
        say("   injected failure after create_card; Temporal retried the activity")
        say(f"   cards at provider for this purchase: {len(cards)} (retry adopted it, no duplicate)")
        provider.settle(provider.authorize(card, "acme-coffee", "15.00").token)
        say("   merchant charges $15.00")
        await finish(h, budget_id, ns)

        b = await get_budget(budget_id)
        open_cards = [c for c in await provider.list_cards() if c.status != "closed"]
        say("Summary")
        say(f"   spent ${b.spent} of ${b.limit_amount}, nothing left on hold: {b.reserved == Decimal('0')}")
        say(f"   open cards remaining: {len(open_cards)}")


if __name__ == "__main__":
    asyncio.run(main())
