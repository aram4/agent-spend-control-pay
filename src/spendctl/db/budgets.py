"""Each operation is one transaction that locks the budget row, then the
reservation, and is idempotent per purchase_id."""

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from spendctl.db.engine import get_sessionmaker
from spendctl.db.models import Budget, Reservation
from spendctl.money import to_money

DEFAULT_RESERVATION_TTL = timedelta(minutes=30)
UNIQUE_VIOLATION = "23505"


class BudgetError(Exception):
    pass


class BudgetNotFound(BudgetError):
    pass


class ReservationNotFound(BudgetError):
    pass


class ReservationMismatch(BudgetError):
    pass


class ReservationReleased(BudgetError):
    pass


class CaptureExceedsReservation(BudgetError):
    pass


class ReserveStatus(enum.StrEnum):
    RESERVED = "reserved"
    EXISTING = "existing"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class ReservationView:
    id: uuid.UUID
    purchase_id: uuid.UUID
    budget_id: uuid.UUID
    amount: Decimal
    captured_amount: Decimal | None
    status: str
    expires_at: datetime


@dataclass(frozen=True)
class ReserveResult:
    status: ReserveStatus
    reservation: ReservationView | None
    available: Decimal


@dataclass(frozen=True)
class BudgetView:
    id: uuid.UUID
    user_id: str
    limit_amount: Decimal
    spent: Decimal
    reserved: Decimal

    @property
    def available(self) -> Decimal:
        return self.limit_amount - self.spent - self.reserved


def _view(r: Reservation) -> ReservationView:
    return ReservationView(
        id=r.id,
        purchase_id=r.purchase_id,
        budget_id=r.budget_id,
        amount=r.amount,
        captured_amount=r.captured_amount,
        status=r.status,
        expires_at=r.expires_at,
    )


async def create_budget(
    user_id: str,
    limit_amount: Decimal | int | str,
    *,
    window_type: str = "task",
    window_seconds: int | None = None,
    sessionmaker: async_sessionmaker | None = None,
) -> uuid.UUID:
    sm = sessionmaker or get_sessionmaker()
    budget = Budget(
        user_id=user_id,
        limit_amount=to_money(limit_amount),
        window_type=window_type,
        window_seconds=window_seconds,
    )
    async with sm() as session, session.begin():
        session.add(budget)
    return budget.id


async def get_budget(budget_id: uuid.UUID, *, sessionmaker: async_sessionmaker | None = None) -> BudgetView:
    sm = sessionmaker or get_sessionmaker()
    async with sm() as session:
        b = await session.get(Budget, budget_id)
        if b is None:
            raise BudgetNotFound(budget_id)
        return BudgetView(b.id, b.user_id, b.limit_amount, b.spent, b.reserved)


async def reserve(
    purchase_id: uuid.UUID,
    budget_id: uuid.UUID,
    amount: Decimal | int | str,
    *,
    ttl: timedelta = DEFAULT_RESERVATION_TTL,
    sessionmaker: async_sessionmaker | None = None,
) -> ReserveResult:
    amount = to_money(amount)
    if amount <= 0:
        raise ValueError("reservation amount must be positive")
    sm = sessionmaker or get_sessionmaker()
    try:
        return await _reserve_once(sm, purchase_id, budget_id, amount, ttl)
    except IntegrityError as e:
        # Lost a race for the same purchase_id on another budget; re-run to see the winner.
        if getattr(e.orig, "sqlstate", None) != UNIQUE_VIOLATION:
            raise
        return await _reserve_once(sm, purchase_id, budget_id, amount, ttl)


async def _reserve_once(
    sm: async_sessionmaker,
    purchase_id: uuid.UUID,
    budget_id: uuid.UUID,
    amount: Decimal,
    ttl: timedelta,
) -> ReserveResult:
    async with sm() as session, session.begin():
        budget = await session.get(Budget, budget_id, with_for_update=True)
        if budget is None:
            raise BudgetNotFound(budget_id)
        available = budget.limit_amount - budget.spent - budget.reserved

        existing = await _find(session, purchase_id)
        if existing is not None:
            if existing.budget_id != budget_id or existing.amount != amount:
                raise ReservationMismatch(
                    f"purchase {purchase_id} already reserved {existing.amount} "
                    f"on budget {existing.budget_id}"
                )
            return ReserveResult(ReserveStatus.EXISTING, _view(existing), available)

        if available < amount:
            return ReserveResult(ReserveStatus.INSUFFICIENT, None, available)

        reservation = Reservation(
            purchase_id=purchase_id,
            budget_id=budget_id,
            amount=amount,
            status="held",
            expires_at=datetime.now(UTC) + ttl,
        )
        session.add(reservation)
        budget.reserved += amount
        await session.flush()
        return ReserveResult(ReserveStatus.RESERVED, _view(reservation), available - amount)


async def release(
    purchase_id: uuid.UUID, *, sessionmaker: async_sessionmaker | None = None
) -> ReservationView | None:
    sm = sessionmaker or get_sessionmaker()
    async with sm() as session, session.begin():
        locked = await _lock(session, purchase_id)
        if locked is None:
            return None
        budget, reservation = locked
        if reservation.status == "held":
            reservation.status = "released"
            budget.reserved -= reservation.amount
        return _view(reservation)


async def capture(
    purchase_id: uuid.UUID,
    final_amount: Decimal | int | str,
    *,
    sessionmaker: async_sessionmaker | None = None,
) -> ReservationView:
    """Spend `final_amount` and free the rest. It can't exceed the hold: the card limit is the hold."""
    final_amount = to_money(final_amount)
    if final_amount < 0:
        raise ValueError("capture amount must be non-negative")
    sm = sessionmaker or get_sessionmaker()
    async with sm() as session, session.begin():
        locked = await _lock(session, purchase_id)
        if locked is None:
            raise ReservationNotFound(purchase_id)
        budget, reservation = locked

        if reservation.status == "captured":
            if reservation.captured_amount != final_amount:
                raise ReservationMismatch(
                    f"purchase {purchase_id} already captured {reservation.captured_amount}, "
                    f"not {final_amount}"
                )
            return _view(reservation)
        if reservation.status == "released":
            raise ReservationReleased(purchase_id)
        if final_amount > reservation.amount:
            raise CaptureExceedsReservation(
                f"capture {final_amount} > reservation {reservation.amount} for {purchase_id}"
            )

        reservation.status = "captured"
        reservation.captured_amount = final_amount
        budget.reserved -= reservation.amount
        budget.spent += final_amount
        return _view(reservation)


async def _find(session: AsyncSession, purchase_id: uuid.UUID) -> Reservation | None:
    return await session.scalar(select(Reservation).where(Reservation.purchase_id == purchase_id))


async def _lock(session: AsyncSession, purchase_id: uuid.UUID) -> tuple[Budget, Reservation] | None:
    # budget_id never changes after insert, so reading it unlocked is safe.
    budget_id = await session.scalar(
        select(Reservation.budget_id).where(Reservation.purchase_id == purchase_id)
    )
    if budget_id is None:
        return None
    budget = await session.get(Budget, budget_id, with_for_update=True)
    reservation = await session.scalar(
        select(Reservation).where(Reservation.purchase_id == purchase_id).with_for_update()
    )
    return budget, reservation
