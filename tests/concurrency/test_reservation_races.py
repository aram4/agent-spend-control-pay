import asyncio
import random
import uuid
from collections import Counter
from decimal import Decimal

from spendctl.db import budgets
from spendctl.db.budgets import ReserveStatus
from tests.invariants import assert_all_invariants

D = Decimal


async def test_20_parallel_reserves_never_overspend():
    bid = await budgets.create_budget("user-1", "200")
    results = await asyncio.gather(*(budgets.reserve(uuid.uuid4(), bid, "25") for _ in range(20)))

    statuses = Counter(r.status for r in results)
    assert statuses == {ReserveStatus.RESERVED: 8, ReserveStatus.INSUFFICIENT: 12}
    b = await budgets.get_budget(bid)
    assert (b.reserved, b.spent) == (D("200"), D("0"))
    await assert_all_invariants([bid])


async def test_parallel_reserves_for_same_purchase_create_one_reservation():
    bid, pid = await budgets.create_budget("user-1", "200"), uuid.uuid4()
    results = await asyncio.gather(*(budgets.reserve(pid, bid, "25") for _ in range(10)))

    statuses = Counter(r.status for r in results)
    assert statuses == {ReserveStatus.RESERVED: 1, ReserveStatus.EXISTING: 9}
    assert len({r.reservation.id for r in results}) == 1
    assert (await budgets.get_budget(bid)).reserved == D("25")
    await assert_all_invariants([bid])


async def test_parallel_releases_release_once():
    bid, pid = await budgets.create_budget("user-1", "200"), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    await budgets.reserve(uuid.uuid4(), bid, "40")
    await asyncio.gather(*(budgets.release(pid) for _ in range(10)))

    assert (await budgets.get_budget(bid)).reserved == D("40")
    await assert_all_invariants([bid])


async def test_parallel_captures_capture_once():
    bid, pid = await budgets.create_budget("user-1", "200"), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    await asyncio.gather(*(budgets.capture(pid, "22") for _ in range(10)))

    b = await budgets.get_budget(bid)
    assert (b.spent, b.reserved) == (D("22"), D("0"))
    await assert_all_invariants([bid])


async def test_capture_racing_release_has_exactly_one_winner():
    bid, pid = await budgets.create_budget("user-1", "200"), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    capture_result, _ = await asyncio.gather(
        budgets.capture(pid, "25"), budgets.release(pid), return_exceptions=True
    )

    b = await budgets.get_budget(bid)
    if isinstance(capture_result, budgets.ReservationReleased):
        assert (b.spent, b.reserved) == (D("0"), D("0"))
    else:
        assert not isinstance(capture_result, BaseException), capture_result
        assert (b.spent, b.reserved) == (D("25"), D("0"))
    await assert_all_invariants([bid])


async def test_random_concurrent_churn_preserves_invariants():
    rng = random.Random()
    bid = await budgets.create_budget("user-1", "500")
    purchases = [uuid.uuid4() for _ in range(15)]
    amounts = {p: D(rng.randint(1, 80)) for p in purchases}

    async def lifecycle(pid: uuid.UUID) -> None:
        for _ in range(rng.randint(1, 3)):
            result = await budgets.reserve(pid, bid, amounts[pid])
        if result.status is ReserveStatus.INSUFFICIENT:
            return
        await asyncio.sleep(rng.random() / 100)
        if rng.random() < 0.5:
            final = amounts[pid] - D(rng.randint(0, 5)) if amounts[pid] > 5 else amounts[pid]
            for _ in range(rng.randint(1, 3)):
                await budgets.capture(pid, final)
        for _ in range(rng.randint(1, 3)):
            await budgets.release(pid)

    await asyncio.gather(*(lifecycle(p) for p in purchases))

    b = await budgets.get_budget(bid)
    assert b.reserved == D("0")
    await assert_all_invariants([bid])


async def test_same_purchase_racing_on_two_budgets_reserves_on_one(monkeypatch):
    attempts = 0
    real_reserve_once = budgets._reserve_once

    async def counting_reserve_once(*args):
        nonlocal attempts
        attempts += 1
        return await real_reserve_once(*args)

    monkeypatch.setattr(budgets, "_reserve_once", counting_reserve_once)
    a, b = await budgets.create_budget("user-1", "500"), await budgets.create_budget("user-1", "500")

    rounds = 10
    for _ in range(rounds):
        pid = uuid.uuid4()
        results = await asyncio.gather(
            budgets.reserve(pid, a, "25"), budgets.reserve(pid, b, "25"), return_exceptions=True
        )
        ok = [r for r in results if not isinstance(r, BaseException)]
        assert [r.status for r in ok] == [ReserveStatus.RESERVED], results
        assert sum(isinstance(r, budgets.ReservationMismatch) for r in results) == 1, results

    totals = [(await budgets.get_budget(x)).reserved for x in (a, b)]
    assert sum(totals) == D("25") * rounds
    assert attempts > 2 * rounds, "unique-violation retry path never ran"
    await assert_all_invariants([a, b])
