import uuid
from datetime import timedelta

import pytest
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from spendctl.db.purchases import create_purchase
from spendctl.providers.mock import FaultMode, MockCardProvider
from spendctl.workflows.activities import CardActivities, CreateCardInput, IssuedCard
from tests.invariants import assert_card_invariants
from tests.unit.test_issue_card import recorded_cards


@workflow.defn(sandboxed=False)
class IssueCardWorkflow:
    @workflow.run
    async def run(self, input: CreateCardInput) -> IssuedCard:
        return await workflow.execute_activity_method(
            CardActivities.create_card,
            input,
            start_to_close_timeout=timedelta(seconds=1),
            retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=50), maximum_attempts=5),
        )


@pytest.fixture(scope="module")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def run_issue(env, provider, purchase, merchant="acme", limit="50") -> IssuedCard:
    queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[IssueCardWorkflow],
        activities=[CardActivities(provider).create_card],
    ):
        return await env.client.execute_workflow(
            IssueCardWorkflow.run,
            CreateCardInput(str(purchase.id), merchant, limit),
            id=purchase.workflow_id,
            task_queue=queue,
        )


@pytest.mark.parametrize(
    "method, mode",
    [
        ("create_card", FaultMode.FAIL_AFTER),
        ("create_card", FaultMode.FAIL_BEFORE),
        ("create_card", FaultMode.HANG_AFTER),
        ("create_card", FaultMode.HANG_BEFORE),
        ("find_card_by_key", FaultMode.FAIL_BEFORE),
        ("find_card_by_key", FaultMode.HANG_BEFORE),
    ],
)
async def test_retried_activity_yields_exactly_one_card(env, method, mode):
    provider, purchase = MockCardProvider(), await create_purchase()
    provider.inject(method, mode)

    issued = await run_issue(env, provider, purchase)

    assert [c.token for c in await provider.list_cards()] == [issued.token]
    assert [r.provider_card_token for r in await recorded_cards(purchase.id)] == [issued.token]
    assert provider.calls["find_card_by_key"] == 2
    await assert_card_invariants(provider)


async def test_param_mismatch_fails_without_retrying(env):
    provider, purchase = MockCardProvider(), await create_purchase()
    await run_issue(env, provider, purchase, limit="50")

    with pytest.raises(WorkflowFailureError) as exc:
        await run_issue(env, provider, purchase, limit="75")

    cause = exc.value.cause
    assert isinstance(cause, ActivityError) and isinstance(cause.cause, ApplicationError)
    assert cause.cause.type == "CardParamMismatch" and cause.cause.non_retryable
    assert provider.calls["find_card_by_key"] == 2
    assert len(await provider.list_cards()) == 1
