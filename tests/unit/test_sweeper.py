import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from spendctl.cards import card_idem_key, issue_card
from spendctl.db.engine import get_sessionmaker
from spendctl.db.models import CardRecord
from spendctl.db.purchases import create_purchase, set_state
from spendctl.providers.base import CardStatus
from spendctl.providers.mock import FaultMode, MockCardProvider
from spendctl.sweeper import sweep_orphan_cards
from tests.invariants import assert_card_invariants

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
LATER = T0 + timedelta(hours=1)


def provider() -> MockCardProvider:
    return MockCardProvider(clock=lambda: T0)


async def status(p: MockCardProvider, token: str) -> CardStatus:
    return next(c.status for c in await p.list_cards() if c.token == token)


async def issue(p, purchase):
    return await issue_card(
        p, purchase_id=purchase.id, workflow_id=purchase.workflow_id, merchant_lock="acme", limit="50"
    )


async def test_closes_injected_orphan_and_logs_it(caplog):
    p = provider()
    orphan = await p.create_card("purchase-does-not-exist-card", "acme", D("50"))
    with caplog.at_level(logging.WARNING, logger="spendctl.sweeper"):
        result = await sweep_orphan_cards(p, now=LATER)
    assert result.closed == {orphan.token: "no matching purchase"}
    assert await status(p, orphan.token) is CardStatus.CLOSED
    [record] = [r for r in caplog.records if r.getMessage() == "closed orphan card"]
    assert record.card_token == orphan.token
    await assert_card_invariants(p)


async def test_young_unrecorded_card_is_left_for_its_create_attempt():
    p = provider()
    card = await p.create_card("purchase-does-not-exist-card", "acme", D("50"))
    result = await sweep_orphan_cards(p, now=T0 + timedelta(minutes=5))
    assert result.closed == {} and await status(p, card.token) is CardStatus.OPEN


async def test_recorded_card_of_active_purchase_is_kept():
    p, purchase = provider(), await create_purchase()
    card = await issue(p, purchase)
    assert (await sweep_orphan_cards(p, now=LATER)).closed == {}
    assert await status(p, card.token) is CardStatus.OPEN


async def test_recorded_card_of_terminal_purchase_is_closed_without_grace():
    p, purchase = provider(), await create_purchase()
    card = await issue(p, purchase)
    await set_state(purchase.id, "rejected")
    result = await sweep_orphan_cards(p, now=T0)
    assert result.closed == {card.token: "purchase is rejected"}
    async with get_sessionmaker()() as session:
        rec = await session.scalar(select(CardRecord).where(CardRecord.provider_card_token == card.token))
    assert rec.status == "closed"
    await assert_card_invariants(p)


async def test_unrecorded_card_of_active_purchase_is_in_flight():
    p, purchase = provider(), await create_purchase()
    card = await p.create_card(card_idem_key(purchase.workflow_id), "acme", D("50"))
    assert (await sweep_orphan_cards(p, now=LATER)).closed == {}
    assert await status(p, card.token) is CardStatus.OPEN


async def test_unrecorded_card_of_terminal_purchase_is_closed():
    p, purchase = provider(), await create_purchase()
    card = await p.create_card(card_idem_key(purchase.workflow_id), "acme", D("50"))
    await set_state(purchase.id, "expired")
    assert (await sweep_orphan_cards(p, now=LATER)).closed == {
        card.token: "unrecorded card for expired purchase"
    }


async def test_duplicate_card_for_purchase_is_closed():
    p, purchase = provider(), await create_purchase()
    kept = await issue(p, purchase)
    dupe = await p.create_card(card_idem_key(purchase.workflow_id), "acme", D("50"))
    result = await sweep_orphan_cards(p, now=LATER)
    assert result.closed == {dupe.token: "duplicate card for purchase"}
    assert await status(p, kept.token) is CardStatus.OPEN


async def test_close_failure_is_reported_and_next_sweep_finishes():
    p = provider()
    a = await p.create_card("purchase-gone-a-card", "acme", D("50"))
    b = await p.create_card("purchase-gone-b-card", "acme", D("50"))
    p.inject("close_card", FaultMode.FAIL_BEFORE)
    first = await sweep_orphan_cards(p, now=LATER)
    assert set(first.errors) == {a.token} and set(first.closed) == {b.token}
    second = await sweep_orphan_cards(p, now=LATER)
    assert set(second.closed) == {a.token} and second.errors == {}
    await assert_card_invariants(p)
