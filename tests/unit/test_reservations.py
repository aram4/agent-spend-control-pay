import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from spendctl.db import budgets
from spendctl.db.budgets import ReserveStatus
from spendctl.db.engine import get_sessionmaker
from spendctl.db.models import Reservation
from tests.invariants import assert_all_invariants

D = Decimal


async def new_budget(limit="200") -> uuid.UUID:
    return await budgets.create_budget("user-1", limit)


async def reservation_count(purchase_id: uuid.UUID) -> int:
    async with get_sessionmaker()() as session:
        return await session.scalar(
            select(func.count()).select_from(Reservation).where(Reservation.purchase_id == purchase_id)
        )


async def test_reserve_holds_funds():
    bid, pid = await new_budget(), uuid.uuid4()
    result = await budgets.reserve(pid, bid, "25")
    assert result.status is ReserveStatus.RESERVED
    assert result.reservation.status == "held" and result.reservation.amount == D("25.00")
    assert result.available == D("175.00")
    b = await budgets.get_budget(bid)
    assert (b.reserved, b.spent, b.available) == (D("25"), D("0"), D("175"))
    await assert_all_invariants([bid])


async def test_reserve_twice_is_idempotent():
    bid, pid = await new_budget(), uuid.uuid4()
    first = await budgets.reserve(pid, bid, "25")
    second = await budgets.reserve(pid, bid, "25")
    assert first.status is ReserveStatus.RESERVED
    assert second.status is ReserveStatus.EXISTING
    assert second.reservation == first.reservation
    assert await reservation_count(pid) == 1
    assert (await budgets.get_budget(bid)).reserved == D("25")
    await assert_all_invariants([bid])


async def test_reserve_after_release_returns_released_reservation_unchanged():
    bid, pid = await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    await budgets.release(pid)
    again = await budgets.reserve(pid, bid, "25")
    assert again.status is ReserveStatus.EXISTING and again.reservation.status == "released"
    assert (await budgets.get_budget(bid)).reserved == D("0")


async def test_reserve_same_purchase_different_amount_raises():
    bid, pid = await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    with pytest.raises(budgets.ReservationMismatch):
        await budgets.reserve(pid, bid, "30")
    assert (await budgets.get_budget(bid)).reserved == D("25")


async def test_reserve_same_purchase_different_budget_raises():
    bid, other, pid = await new_budget(), await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    with pytest.raises(budgets.ReservationMismatch):
        await budgets.reserve(pid, other, "25")
    assert (await budgets.get_budget(other)).reserved == D("0")


async def test_reserve_can_fill_budget_exactly():
    bid = await new_budget("50")
    assert (await budgets.reserve(uuid.uuid4(), bid, "25")).status is ReserveStatus.RESERVED
    assert (await budgets.reserve(uuid.uuid4(), bid, "25")).status is ReserveStatus.RESERVED
    assert (await budgets.reserve(uuid.uuid4(), bid, "0.01")).status is ReserveStatus.INSUFFICIENT
    await assert_all_invariants([bid])


async def test_insufficient_leaves_budget_unchanged_and_creates_nothing():
    bid, pid = await new_budget("20"), uuid.uuid4()
    result = await budgets.reserve(pid, bid, "25")
    assert result.status is ReserveStatus.INSUFFICIENT and result.reservation is None
    assert result.available == D("20")
    assert (await budgets.get_budget(bid)).reserved == D("0")
    assert await reservation_count(pid) == 0


async def test_reserve_unknown_budget_raises():
    with pytest.raises(budgets.BudgetNotFound):
        await budgets.reserve(uuid.uuid4(), uuid.uuid4(), "1")


@pytest.mark.parametrize("amount", [0, "-1", "0.001"])
async def test_reserve_rejects_bad_amounts(amount):
    with pytest.raises(ValueError):
        await budgets.reserve(uuid.uuid4(), await new_budget(), amount)


async def test_reserve_rejects_float():
    with pytest.raises(TypeError):
        await budgets.reserve(uuid.uuid4(), await new_budget(), 25.0)


async def test_double_release_releases_once():
    bid, pid = await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    await budgets.reserve(uuid.uuid4(), bid, "10")
    first = await budgets.release(pid)
    second = await budgets.release(pid)
    assert first.status == second.status == "released"
    assert (await budgets.get_budget(bid)).reserved == D("10")
    await assert_all_invariants([bid])


async def test_release_without_reservation_is_noop():
    assert await budgets.release(uuid.uuid4()) is None


async def test_capture_moves_final_amount_to_spent_and_frees_the_rest():
    bid, pid = await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    view = await budgets.capture(pid, "21.50")
    assert view.status == "captured" and view.captured_amount == D("21.50")
    b = await budgets.get_budget(bid)
    assert (b.spent, b.reserved, b.available) == (D("21.50"), D("0"), D("178.50"))
    await assert_all_invariants([bid])


async def test_double_capture_is_idempotent():
    bid, pid = await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    await budgets.capture(pid, "25")
    await budgets.capture(pid, "25")
    b = await budgets.get_budget(bid)
    assert (b.spent, b.reserved) == (D("25"), D("0"))


async def test_recapture_with_different_amount_raises():
    bid, pid = await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    await budgets.capture(pid, "20")
    with pytest.raises(budgets.ReservationMismatch):
        await budgets.capture(pid, "25")
    assert (await budgets.get_budget(bid)).spent == D("20")


async def test_capture_cannot_exceed_reservation():
    bid, pid = await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    with pytest.raises(budgets.CaptureExceedsReservation):
        await budgets.capture(pid, "25.01")
    assert (await budgets.get_budget(bid)).reserved == D("25")


async def test_capture_after_release_raises():
    bid, pid = await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    await budgets.release(pid)
    with pytest.raises(budgets.ReservationReleased):
        await budgets.capture(pid, "25")
    assert (await budgets.get_budget(bid)).spent == D("0")


async def test_release_after_capture_changes_nothing():
    bid, pid = await new_budget(), uuid.uuid4()
    await budgets.reserve(pid, bid, "25")
    await budgets.capture(pid, "20")
    view = await budgets.release(pid)
    assert view.status == "captured"
    b = await budgets.get_budget(bid)
    assert (b.spent, b.reserved) == (D("20"), D("0"))


async def test_capture_unknown_purchase_raises():
    with pytest.raises(budgets.ReservationNotFound):
        await budgets.capture(uuid.uuid4(), "1")


async def test_db_check_rejects_overcommitted_budget():
    bid = await new_budget("100")
    async with get_sessionmaker()() as session:
        with pytest.raises(IntegrityError, match="spent_plus_reserved_within_limit"):
            async with session.begin():
                await session.execute(
                    text("UPDATE budgets SET spent = 60, reserved = 50 WHERE id = :id"), {"id": bid}
                )
