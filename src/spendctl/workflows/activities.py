import uuid
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from spendctl.cards import CardIssueError, issue_card
from spendctl.db import budgets
from spendctl.db.engine import get_sessionmaker
from spendctl.db.models import CardRecord
from spendctl.db.purchases import set_state
from spendctl.providers.base import CardNotFound, CardProvider, CardStatus


def _non_retryable(e: Exception) -> ApplicationError:
    return ApplicationError(str(e), type=type(e).__name__, non_retryable=True)


@dataclass(frozen=True)
class CreateCardInput:
    purchase_id: str
    merchant_lock: str
    limit: str


@dataclass(frozen=True)
class IssuedCard:
    token: str
    merchant_lock: str
    limit: str


@dataclass(frozen=True)
class TxnView:
    token: str
    status: str
    merchant: str
    amount: str
    settled_amount: str | None


class CardActivities:
    def __init__(self, provider: CardProvider, sessionmaker: async_sessionmaker | None = None) -> None:
        self._provider = provider
        self._sessionmaker = sessionmaker

    @activity.defn
    async def create_card(self, input: CreateCardInput) -> IssuedCard:
        try:
            card = await issue_card(
                self._provider,
                purchase_id=uuid.UUID(input.purchase_id),
                workflow_id=activity.info().workflow_id,
                merchant_lock=input.merchant_lock,
                limit=input.limit,
                sessionmaker=self._sessionmaker,
            )
        except (CardIssueError, ValueError) as e:
            raise _non_retryable(e) from e
        return IssuedCard(card.token, card.merchant_lock, str(card.limit))

    @activity.defn
    async def close_card(self, token: str) -> None:
        """Idempotent: closing a closed card is a no-op at the provider."""
        try:
            card = await self._provider.close_card(token)
        except CardNotFound as e:
            raise _non_retryable(e) from e
        if card.status is not CardStatus.CLOSED:
            raise RuntimeError(f"provider reports {card.status} after closing {token}")
        async with (self._sessionmaker or get_sessionmaker())() as session, session.begin():
            await session.execute(
                update(CardRecord)
                .where(CardRecord.provider_card_token == token)
                .values(status=str(CardStatus.CLOSED))
            )

    @activity.defn
    async def card_transactions(self, token: str) -> list[TxnView]:
        """The provider's record of what was charged; never the agent's account of it."""
        return [
            TxnView(
                t.token,
                str(t.status),
                t.merchant,
                str(t.amount),
                None if t.settled_amount is None else str(t.settled_amount),
            )
            for t in await self._provider.list_transactions()
            if t.card_token == token
        ]


@dataclass(frozen=True)
class ReserveInput:
    purchase_id: str
    budget_id: str
    amount: str


@dataclass(frozen=True)
class ReserveOutcome:
    ok: bool
    available: str


@dataclass(frozen=True)
class CaptureInput:
    purchase_id: str
    amount: str


@dataclass(frozen=True)
class SetStateInput:
    purchase_id: str
    state: str


class PurchaseActivities:
    def __init__(self, sessionmaker: async_sessionmaker | None = None) -> None:
        self._sessionmaker = sessionmaker

    @activity.defn
    async def reserve_budget(self, input: ReserveInput) -> ReserveOutcome:
        try:
            result = await budgets.reserve(
                uuid.UUID(input.purchase_id),
                uuid.UUID(input.budget_id),
                input.amount,
                sessionmaker=self._sessionmaker,
            )
        except (budgets.BudgetError, ValueError) as e:
            raise _non_retryable(e) from e
        return ReserveOutcome(result.status is not budgets.ReserveStatus.INSUFFICIENT, str(result.available))

    @activity.defn
    async def release_budget(self, purchase_id: str) -> None:
        await budgets.release(uuid.UUID(purchase_id), sessionmaker=self._sessionmaker)

    @activity.defn
    async def capture_budget(self, input: CaptureInput) -> None:
        try:
            await budgets.capture(
                uuid.UUID(input.purchase_id), input.amount, sessionmaker=self._sessionmaker
            )
        except (budgets.BudgetError, ValueError) as e:
            raise _non_retryable(e) from e

    @activity.defn
    async def set_purchase_state(self, input: SetStateInput) -> None:
        await set_state(uuid.UUID(input.purchase_id), input.state, sessionmaker=self._sessionmaker)


def all_activities(provider: CardProvider, sessionmaker: async_sessionmaker | None = None) -> list:
    cards, purchases = CardActivities(provider, sessionmaker), PurchaseActivities(sessionmaker)
    return [
        cards.create_card,
        cards.close_card,
        cards.card_transactions,
        purchases.reserve_budget,
        purchases.release_budget,
        purchases.capture_budget,
        purchases.set_purchase_state,
    ]
