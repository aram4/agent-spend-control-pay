from decimal import Decimal

import pytest
from sqlalchemy import select

from spendctl import cards
from spendctl.cards import CardParamMismatch, CardUnusable, card_idem_key, issue_card
from spendctl.db.engine import get_sessionmaker
from spendctl.db.models import CardRecord
from spendctl.db.purchases import create_purchase
from spendctl.providers.mock import FaultMode, InjectedFault, MockCardProvider
from tests.invariants import assert_card_invariants

D = Decimal


async def recorded_cards(purchase_id) -> list[CardRecord]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(CardRecord).where(CardRecord.purchase_id == purchase_id)))


async def issue(provider, purchase, merchant="acme", limit="50"):
    return await issue_card(
        provider,
        purchase_id=purchase.id,
        workflow_id=purchase.workflow_id,
        merchant_lock=merchant,
        limit=limit,
    )


async def test_issues_and_records_card():
    p, purchase = MockCardProvider(), await create_purchase()
    card = await issue(p, purchase)
    assert card.idem_key == card_idem_key(purchase.workflow_id)
    [rec] = await recorded_cards(purchase.id)
    assert (rec.provider_card_token, rec.limit_amount, rec.status) == (card.token, D("50"), "open")
    await assert_card_invariants(p)


async def test_second_call_adopts_existing_card():
    p, purchase = MockCardProvider(), await create_purchase()
    first = await issue(p, purchase)
    second = await issue(p, purchase)
    assert first == second
    assert p.calls["create_card"] == 1 and len(await p.list_cards()) == 1
    assert len(await recorded_cards(purchase.id)) == 1


async def test_create_succeeds_at_provider_but_errors_then_retry_adopts():
    p, purchase = MockCardProvider(), await create_purchase()
    p.inject("create_card", FaultMode.FAIL_AFTER)
    with pytest.raises(InjectedFault):
        await issue(p, purchase)
    assert len(await p.list_cards()) == 1
    assert await recorded_cards(purchase.id) == []

    card = await issue(p, purchase)
    assert [c.token for c in await p.list_cards()] == [card.token]
    assert len(await recorded_cards(purchase.id)) == 1
    await assert_card_invariants(p)


async def test_db_write_fails_after_create_then_retry_adopts(monkeypatch):
    p, purchase = MockCardProvider(), await create_purchase()
    real_record = cards._record

    async def failing_record(*args):
        monkeypatch.setattr(cards, "_record", real_record)
        raise ConnectionError("db went away")

    monkeypatch.setattr(cards, "_record", failing_record)
    with pytest.raises(ConnectionError):
        await issue(p, purchase)
    await issue(p, purchase)
    assert len(await p.list_cards()) == 1 and len(await recorded_cards(purchase.id)) == 1


@pytest.mark.parametrize("merchant, limit", [("other-shop", "50"), ("acme", "60")])
async def test_existing_card_with_different_params_is_refused(merchant, limit):
    p, purchase = MockCardProvider(), await create_purchase()
    await issue(p, purchase)
    with pytest.raises(CardParamMismatch):
        await issue(p, purchase, merchant=merchant, limit=limit)
    assert len(await p.list_cards()) == 1


async def test_existing_closed_card_is_not_adopted():
    p, purchase = MockCardProvider(), await create_purchase()
    card = await issue(p, purchase)
    await p.close_card(card.token)
    with pytest.raises(CardUnusable):
        await issue(p, purchase)
    assert p.calls["create_card"] == 1


@pytest.mark.parametrize("limit", ["0", "-5"])
async def test_refuses_card_without_positive_limit(limit):
    p, purchase = MockCardProvider(), await create_purchase()
    with pytest.raises(ValueError):
        await issue(p, purchase, limit=limit)
    assert p.calls["create_card"] == 0

