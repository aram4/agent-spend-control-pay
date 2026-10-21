import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Money = Numeric(12, 2)


PURCHASE_STATES = (
    "requested",
    "reserved",
    "pending_approval",
    "approved",
    "card_issued",
    "authorized",
    "settled",
    "rejected",
    "expired",
    "refunded",
    "partially_refunded",
)
TERMINAL_PURCHASE_STATES = frozenset(
    {"settled", "rejected", "expired", "refunded", "partially_refunded"}
)


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (
        CheckConstraint("spent + reserved <= limit_amount", name="spent_plus_reserved_within_limit"),
        CheckConstraint("limit_amount >= 0", name="limit_nonnegative"),
        CheckConstraint("spent >= 0", name="spent_nonnegative"),
        CheckConstraint("reserved >= 0", name="reserved_nonnegative"),
        CheckConstraint("window_type IN ('task', 'rolling')", name="window_type_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String, index=True)
    limit_amount: Mapped[Decimal] = mapped_column(Money)
    spent: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), server_default="0")
    reserved: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), server_default="0")
    window_type: Mapped[str] = mapped_column(String, default="task", server_default="task")
    window_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status IN ('held', 'captured', 'released')", name="status_valid"),
        CheckConstraint(
            "(status = 'captured') = (captured_amount IS NOT NULL)",
            name="captured_amount_iff_captured",
        ),
        CheckConstraint(
            "captured_amount IS NULL OR (captured_amount >= 0 AND captured_amount <= amount)",
            name="captured_within_reservation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    budget_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("budgets.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    captured_amount: Mapped[Decimal | None] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String, default="held", server_default="held")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (CheckConstraint(_in("state", PURCHASE_STATES), name="state_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[str] = mapped_column(String, unique=True)
    state: Mapped[str] = mapped_column(String, default="requested", server_default="requested")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CardRecord(Base):
    __tablename__ = "cards"
    __table_args__ = (
        CheckConstraint("limit_amount > 0", name="limit_positive"),
        CheckConstraint("status IN ('open', 'paused', 'closed')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    purchase_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchases.id"), unique=True)
    provider_card_token: Mapped[str] = mapped_column(String, unique=True)
    idem_key: Mapped[str] = mapped_column(String, unique=True)
    merchant_lock: Mapped[str] = mapped_column(String)
    limit_amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String, default="open", server_default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
