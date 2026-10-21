import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from spendctl.providers.base import CardNotFound, CardStatus, TxnStatus
from spendctl.providers.mock import (
    EventType,
    FaultMode,
    InjectedFault,
    MockCardProvider,
)

D = Decimal


async def test_create_find_close():
    p = MockCardProvider()
    card = await p.create_card("k1", "acme", D("50"))
    assert card.status is CardStatus.OPEN and card.limit == D("50.00")
    assert await p.find_card_by_key("k1") == card
    assert await p.find_card_by_key("missing") is None
    closed = await p.close_card(card.token)
    assert closed.status is CardStatus.CLOSED
    assert (await p.close_card(card.token)).status is CardStatus.CLOSED


async def test_closing_is_permanent():
    p = MockCardProvider()
    card = await p.create_card("k1", "acme", D("50"))
    assert (await p.pause_card(card.token)).status is CardStatus.PAUSED
    await p.close_card(card.token)
    assert (await p.pause_card(card.token)).status is CardStatus.CLOSED


async def test_unknown_card_raises():
    with pytest.raises(CardNotFound):
        await MockCardProvider().close_card("card_nope")


async def test_no_native_idempotency():
    p = MockCardProvider()
    a = await p.create_card("k1", "acme", D("50"))
    b = await p.create_card("k1", "acme", D("50"))
    assert a.token != b.token and len(await p.list_cards()) == 2


async def test_card_has_no_card_number_fields():
    card = await MockCardProvider().create_card("k1", "acme", D("50"))
    assert not {"pan", "cvv", "exp", "expiry", "number"} & set(vars(card))


async def test_authorization_enforces_merchant_lock_and_limit():
    p = MockCardProvider()
    card = await p.create_card("k1", "Acme Store", D("50"))
    assert p.authorize(card.token, "acme store ", "30").status is TxnStatus.AUTHORIZED
    wrong = p.authorize(card.token, "evil-shop", "1")
    assert (wrong.status, wrong.decline_reason) == (TxnStatus.DECLINED, "merchant_mismatch")
    over = p.authorize(card.token, "Acme Store", "20.01")
    assert (over.status, over.decline_reason) == (TxnStatus.DECLINED, "over_limit")
    assert p.authorize(card.token, "Acme Store", "20").status is TxnStatus.AUTHORIZED


async def test_paused_and_closed_cards_decline():
    p = MockCardProvider()
    card = await p.create_card("k1", "acme", D("50"))
    await p.pause_card(card.token)
    assert p.authorize(card.token, "acme", "1").decline_reason == "card_paused"
    await p.close_card(card.token)
    assert p.authorize(card.token, "acme", "1").decline_reason == "card_closed"


async def test_settlement_can_differ_from_authorization():
    p = MockCardProvider()
    card = await p.create_card("k1", "acme", D("50"))
    auth = p.authorize(card.token, "acme", "40")
    settled = p.settle(auth.token, "43.50")
    assert (settled.status, settled.amount, settled.settled_amount) == (
        TxnStatus.SETTLED,
        D("40"),
        D("43.50"),
    )


async def test_settlement_still_lands_after_card_closed():
    p = MockCardProvider()
    card = await p.create_card("k1", "acme", D("50"))
    auth = p.authorize(card.token, "acme", "40")
    await p.close_card(card.token)
    assert p.settle(auth.token).status is TxnStatus.SETTLED


async def test_void_frees_limit():
    p = MockCardProvider()
    card = await p.create_card("k1", "acme", D("50"))
    p.void(p.authorize(card.token, "acme", "50").token)
    assert p.authorize(card.token, "acme", "50").status is TxnStatus.AUTHORIZED


async def test_events_late_and_out_of_order():
    p = MockCardProvider()
    card = await p.create_card("k1", "acme", D("50"))
    auth = p.authorize(card.token, "acme", "10")
    p.settle(auth.token)
    reversed_events = p.take_events(order="reversed")
    assert [e.type for e in reversed_events] == [EventType.SETTLEMENT, EventType.AUTHORIZATION]

    p.void(p.authorize(card.token, "acme", "5").token)
    first = p.take_events(limit=1)
    assert [e.type for e in first] == [EventType.AUTHORIZATION]
    assert [e.type for e in p.take_events()] == [EventType.VOID]
    assert p.take_events() == []


async def test_list_transactions_since():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    p = MockCardProvider(clock=lambda: now)
    card = await p.create_card("k1", "acme", D("50"))
    old = p.authorize(card.token, "acme", "1")
    now += timedelta(hours=1)
    new = p.authorize(card.token, "acme", "2")
    assert [t.token for t in await p.list_transactions(since=now)] == [new.token]
    assert {t.token for t in await p.list_transactions()} == {old.token, new.token}


async def test_fail_before_has_no_side_effect():
    p = MockCardProvider()
    p.inject("create_card", FaultMode.FAIL_BEFORE)
    with pytest.raises(InjectedFault):
        await p.create_card("k1", "acme", D("50"))
    assert await p.list_cards() == []


async def test_fail_after_keeps_side_effect():
    p = MockCardProvider()
    p.inject("create_card", FaultMode.FAIL_AFTER)
    with pytest.raises(InjectedFault):
        await p.create_card("k1", "acme", D("50"))
    assert len(await p.list_cards()) == 1


@pytest.mark.parametrize("mode, cards", [(FaultMode.HANG_BEFORE, 0), (FaultMode.HANG_AFTER, 1)])
async def test_hang_blocks_until_cancelled(mode, cards):
    p = MockCardProvider()
    p.inject("create_card", mode)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(p.create_card("k1", "acme", D("50")), timeout=0.05)
    assert len(await p.list_cards()) == cards


async def test_fault_targets_nth_call_and_fires_once():
    p = MockCardProvider()
    p.inject("close_card", FaultMode.FAIL_BEFORE, on_call=2)
    card = await p.create_card("k1", "acme", D("50"))
    await p.close_card(card.token)
    with pytest.raises(InjectedFault):
        await p.close_card(card.token)
    await p.close_card(card.token)
    assert p.calls["close_card"] == 3


async def test_inject_rejects_unknown_method():
    with pytest.raises(ValueError):
        MockCardProvider().inject("authorize", FaultMode.FAIL_BEFORE)
