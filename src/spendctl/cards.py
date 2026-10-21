import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from spendctl.db.engine import get_sessionmaker
from spendctl.db.models import CardRecord
from spendctl.money import to_money
from spendctl.providers.base import Card, CardProvider, CardStatus


class CardIssueError(Exception):
    """Not retryable: retrying cannot produce a usable card."""


class CardParamMismatch(CardIssueError):
    pass


class CardUnusable(CardIssueError):
    pass


class CardRecordConflict(CardIssueError):
    pass


def card_idem_key(workflow_id: str) -> str:
    return f"{workflow_id}-card"


async def issue_card(
    provider: CardProvider,
    *,
    purchase_id: uuid.UUID,
    workflow_id: str,
    merchant_lock: str,
    limit: Decimal | int | str,
    sessionmaker: async_sessionmaker | None = None,
) -> Card:
    """Create the purchase's card, or adopt the one an earlier attempt created."""
    limit = to_money(limit)
    if limit <= 0:
        raise ValueError("refusing to issue a card without a positive spend limit")
    key = card_idem_key(workflow_id)

    card = await provider.find_card_by_key(key)
    if card is None:
        card = await provider.create_card(key, merchant_lock, limit)
    elif (card.merchant_lock, card.limit) != (merchant_lock, limit):
        raise CardParamMismatch(
            f"{key}: existing card is {card.merchant_lock}/{card.limit}, wanted {merchant_lock}/{limit}"
        )
    elif card.status is not CardStatus.OPEN:
        raise CardUnusable(f"{key}: existing card {card.token} is {card.status}")

    await _record(sessionmaker or get_sessionmaker(), purchase_id, card)
    return card


async def _record(sm: async_sessionmaker, purchase_id: uuid.UUID, card: Card) -> None:
    async with sm() as session, session.begin():
        await session.execute(
            insert(CardRecord)
            .values(
                purchase_id=purchase_id,
                provider_card_token=card.token,
                idem_key=card.idem_key,
                merchant_lock=card.merchant_lock,
                limit_amount=card.limit,
                status=str(card.status),
            )
            .on_conflict_do_nothing()
        )
        recorded = await session.scalar(
            select(CardRecord.provider_card_token).where(CardRecord.idem_key == card.idem_key)
        )
    if recorded != card.token:
        raise CardRecordConflict(
            f"purchase {purchase_id}: recorded card {recorded}, provider card {card.token}"
        )
