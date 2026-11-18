import asyncio
import uuid
from decimal import Decimal

import pytest
from temporalio.testing import WorkflowEnvironment

from spendctl.db.budgets import create_budget, get_budget
from spendctl.providers.base import CardStatus
from spendctl.providers.mock import FaultMode, MockCardProvider
from spendctl.worker import build_worker
from spendctl.workflows.purchase import PurchaseWorkflow
from spendctl.workflows.starter import start_purchase
from tests.invariants import assert_all_invariants
from tests.unit.test_issue_card import recorded_cards

D = Decimal


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest.fixture
def provider():
    return MockCardProvider()


@pytest.fixture
async def worker(env, provider):
    queue = f"test-{uuid.uuid4()}"
    async with build_worker(env.client, task_queue=queue, provider=provider):
        yield queue


async def start(env, queue, budget_id, merchant="acme", amount="50"):
    return await start_purchase(
        env.client, budget_id=budget_id, merchant=merchant, amount=amount, task_queue=queue
    )


async def wait_for_card(handle) -> str:
    for _ in range(200):
        status = await handle.query(PurchaseWorkflow.status)
        if status.card_token:
            return status.card_token
        await asyncio.sleep(0.05)
    raise AssertionError(f"no card issued; workflow is {status.state}")


async def assert_card_closed(provider, token):
    [card] = [c for c in await provider.list_cards() if c.token == token]
    assert card.status is CardStatus.CLOSED


async def test_settles_for_the_amount_the_provider_reports(env, provider, worker):
    budget_id = await create_budget("u", "100")
    handle = await start(env, worker, budget_id, amount="50")

    token = await wait_for_card(handle)
    txn = provider.authorize(token, "acme", "50")
    provider.settle(txn.token, "47.25")  # e.g. a discount applied at settlement
    result = await handle.result()

    assert (result.state, result.captured) == ("settled", "47.25")
    await assert_card_closed(provider, token)
    [rec] = await recorded_cards(uuid.UUID(result.purchase_id))
    assert rec.status == "closed"
    budget = await get_budget(budget_id)
    assert (budget.spent, budget.reserved) == (D("47.25"), D("0"))
    await assert_all_invariants([budget_id], provider)


async def test_over_budget_is_rejected_without_a_card(env, provider, worker):
    budget_id = await create_budget("u", "30")
    result = await (await start(env, worker, budget_id, amount="50")).result()

    assert result.state == "rejected" and "insufficient budget" in result.reason
    assert result.card_token is None and await provider.list_cards() == []
    assert (await get_budget(budget_id)).reserved == 0
    await assert_all_invariants([budget_id], provider)


async def test_no_charge_expires_closes_card_and_releases_funds(env, provider, worker):
    budget_id = await create_budget("u", "100")
    handle = await start(env, worker, budget_id)
    token = await wait_for_card(handle)

    result = await handle.result()  # time-skips past the checkout window

    assert result.state == "expired"
    await assert_card_closed(provider, token)
    budget = await get_budget(budget_id)
    assert (budget.spent, budget.reserved) == (D("0"), D("0"))
    await assert_all_invariants([budget_id], provider)


async def test_wrong_merchant_is_declined_and_purchase_expires(env, provider, worker):
    budget_id = await create_budget("u", "100")
    handle = await start(env, worker, budget_id, merchant="acme")
    token = await wait_for_card(handle)

    declined = provider.authorize(token, "evil-store", "50")
    result = await handle.result()

    assert declined.decline_reason == "merchant_mismatch"
    assert result.state == "expired"
    assert (await get_budget(budget_id)).reserved == 0
    await assert_all_invariants([budget_id], provider)


async def test_card_created_but_response_lost_still_yields_one_card(env, provider, worker):
    provider.inject("create_card", FaultMode.FAIL_AFTER)
    budget_id = await create_budget("u", "100")
    handle = await start(env, worker, budget_id)

    token = await wait_for_card(handle)
    provider.settle(provider.authorize(token, "acme", "50").token)
    result = await handle.result()

    assert result.state == "settled"
    assert [c.token for c in await provider.list_cards()] == [token]
    await assert_all_invariants([budget_id], provider)


async def test_unsettled_authorization_keeps_the_hold(env, provider, worker):
    budget_id = await create_budget("u", "100")
    handle = await start(env, worker, budget_id)
    token = await wait_for_card(handle)

    provider.authorize(token, "acme", "50")
    result = await handle.result()  # time-skips past the settlement window

    assert result.state == "authorized" and "reconciliation" in result.reason
    await assert_card_closed(provider, token)
    assert (await get_budget(budget_id)).reserved == D("50")
    await assert_all_invariants([budget_id], provider)
