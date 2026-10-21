import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from spendctl.db.engine import get_sessionmaker
from spendctl.db.models import TERMINAL_PURCHASE_STATES, CardRecord, Purchase
from spendctl.providers.base import Card, CardProvider, CardStatus

log = logging.getLogger(__name__)

# An unrecorded card younger than this may belong to a create_card attempt that
# hasn't recorded it yet; its retry will adopt it.
DEFAULT_GRACE = timedelta(minutes=10)


@dataclass
class SweepResult:
    closed: dict[str, str] = field(default_factory=dict)  # token -> reason
    errors: dict[str, str] = field(default_factory=dict)


async def sweep_orphan_cards(
    provider: CardProvider,
    *,
    grace: timedelta = DEFAULT_GRACE,
    now: datetime | None = None,
    sessionmaker: async_sessionmaker | None = None,
) -> SweepResult:
    sm = sessionmaker or get_sessionmaker()
    now = now or datetime.now(UTC)

    async with sm() as session:
        purchases = {
            wf: (pid, state)
            for pid, wf, state in await session.execute(
                select(Purchase.id, Purchase.workflow_id, Purchase.state)
            )
        }
        states_by_id = {pid: state for pid, state in purchases.values()}
        recorded = {
            token: pid
            for token, pid in await session.execute(
                select(CardRecord.provider_card_token, CardRecord.purchase_id)
            )
        }
    purchases_with_card = set(recorded.values())

    def reason_to_close(card: Card) -> str | None:
        if card.token in recorded:
            state = states_by_id[recorded[card.token]]
            return f"purchase is {state}" if state in TERMINAL_PURCHASE_STATES else None
        if now - card.created_at < grace:
            return None
        purchase = purchases.get(card.idem_key.removesuffix("-card"))
        if purchase is None:
            return "no matching purchase"
        pid, state = purchase
        if state in TERMINAL_PURCHASE_STATES:
            return f"unrecorded card for {state} purchase"
        if pid in purchases_with_card:
            return "duplicate card for purchase"
        return None

    result = SweepResult()
    for card in await provider.list_cards():
        if card.status is CardStatus.CLOSED or (reason := reason_to_close(card)) is None:
            continue
        try:
            closed = await provider.close_card(card.token)
            if closed.status is not CardStatus.CLOSED:
                raise RuntimeError(f"provider reports {closed.status} after close")
        except Exception as e:
            result.errors[card.token] = repr(e)
            log.error("failed to close orphan card", extra={"card_token": card.token, "error": repr(e)})
            continue
        async with sm() as session, session.begin():
            await session.execute(
                update(CardRecord)
                .where(CardRecord.provider_card_token == card.token)
                .values(status=str(CardStatus.CLOSED))
            )
        result.closed[card.token] = reason
        log.warning(
            "closed orphan card",
            extra={"card_token": card.token, "idem_key": card.idem_key, "reason": reason},
        )
    return result
