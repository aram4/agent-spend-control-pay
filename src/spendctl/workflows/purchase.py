"""PurchaseWorkflow: reserve budget -> issue a single-use card -> wait for the
merchant's charge -> close the card -> capture what settled (or release).

Deterministic: all I/O happens in activities, time comes from workflow.now().
Policy gates, the LLM intent check and human approval slot in before card
issuance in later phases.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from spendctl.workflows.activities import (
        CaptureInput,
        CardActivities,
        CreateCardInput,
        PurchaseActivities,
        ReserveInput,
        SetStateInput,
        TxnView,
    )

ACTIVITY_TIMEOUT = timedelta(seconds=10)
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(milliseconds=200),
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=10,
)


@dataclass(frozen=True)
class PurchaseRequest:
    purchase_id: str
    budget_id: str
    merchant: str
    amount: str
    # How long the agent has to check out before the card is revoked.
    checkout_window_seconds: float = 600
    poll_interval_seconds: float = 5
    # How long to wait for an authorization to settle once the card is closed.
    settlement_window_seconds: float = 3600
    settlement_poll_interval_seconds: float = 60


@dataclass(frozen=True)
class PurchaseStatus:
    purchase_id: str
    state: str
    reason: str
    card_token: str | None
    captured: str | None


def _charged(txns: list[TxnView]) -> bool:
    return any(t.status in ("authorized", "settled") for t in txns)


def _unsettled(txns: list[TxnView]) -> bool:
    return any(t.status == "authorized" for t in txns)


@workflow.defn
class PurchaseWorkflow:
    def __init__(self) -> None:
        self._purchase_id = ""
        self._state = "requested"
        self._reason = ""
        self._card_token: str | None = None
        self._captured: str | None = None

    @workflow.query
    def status(self) -> PurchaseStatus:
        return PurchaseStatus(
            self._purchase_id, self._state, self._reason, self._card_token, self._captured
        )

    @workflow.run
    async def run(self, req: PurchaseRequest) -> PurchaseStatus:
        self._purchase_id = req.purchase_id

        reserved = await self._act(
            PurchaseActivities.reserve_budget, ReserveInput(req.purchase_id, req.budget_id, req.amount)
        )
        if not reserved.ok:
            return await self._finish(
                "rejected", f"insufficient budget: {reserved.available} available, {req.amount} requested"
            )
        await self._transition("reserved", f"held {req.amount}")

        try:
            card = await self._act(
                CardActivities.create_card, CreateCardInput(req.purchase_id, req.merchant, req.amount)
            )
        except ActivityError as e:
            await self._act(PurchaseActivities.release_budget, req.purchase_id)
            return await self._finish("rejected", f"card issuance failed: {e.cause or e}")
        self._card_token = card.token
        await self._transition("card_issued", f"single-use card locked to {req.merchant}, limit {req.amount}")

        txns = await self._poll(
            card.token, req.checkout_window_seconds, req.poll_interval_seconds, _charged
        )
        if _charged(txns):
            await self._transition("authorized", "merchant charged the card")

        # Single-use: revoke the card once it has been charged or checkout timed
        # out, and before any funds are released.
        await self._act(CardActivities.close_card, card.token)

        if _unsettled(txns):
            txns = await self._poll(
                card.token,
                req.settlement_window_seconds,
                req.settlement_poll_interval_seconds,
                lambda ts: not _unsettled(ts),
            )
        if _unsettled(txns):
            # Money is committed at the network; releasing the hold could overspend.
            self._reason = "authorization never settled; reservation kept for reconciliation"
            return self.status()

        settled = sum((Decimal(t.settled_amount) for t in txns if t.status == "settled"), Decimal("0"))
        if settled == 0:
            await self._act(PurchaseActivities.release_budget, req.purchase_id)
            return await self._finish("expired", "no charge before the checkout window closed; funds released")

        await self._act(PurchaseActivities.capture_budget, CaptureInput(req.purchase_id, str(settled)))
        self._captured = str(settled)
        return await self._finish("settled", f"captured {settled}, released the rest of the hold")

    async def _poll(
        self,
        card_token: str,
        window_seconds: float,
        interval_seconds: float,
        done: Callable[[list[TxnView]], bool],
    ) -> list[TxnView]:
        deadline = workflow.now() + timedelta(seconds=window_seconds)
        interval = timedelta(seconds=interval_seconds)
        while True:
            txns = await self._act(CardActivities.card_transactions, card_token)
            if done(txns) or workflow.now() >= deadline:
                return txns
            await workflow.sleep(min(interval, deadline - workflow.now()))

    async def _transition(self, state: str, reason: str) -> None:
        await self._act(PurchaseActivities.set_purchase_state, SetStateInput(self._purchase_id, state))
        self._state, self._reason = state, reason

    async def _finish(self, state: str, reason: str) -> PurchaseStatus:
        await self._transition(state, reason)
        return self.status()

    async def _act(self, fn, arg):
        return await workflow.execute_activity_method(
            fn, arg, start_to_close_timeout=ACTIVITY_TIMEOUT, retry_policy=ACTIVITY_RETRY
        )
