import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from spendctl.cards import CardIssueError, issue_card
from spendctl.providers.base import CardProvider


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
            raise ApplicationError(str(e), type=type(e).__name__, non_retryable=True) from e
        return IssuedCard(card.token, card.merchant_lock, str(card.limit))
