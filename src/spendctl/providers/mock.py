"""In-memory CardProvider with fault injection and a simulated card network.

Like a provider without native idempotency keys, it will happily create two
cards with the same idem_key; preventing that is the caller's job.
"""

import asyncio
import enum
import itertools
import random
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from spendctl.money import to_money
from spendctl.providers.base import (
    Card,
    CardNotFound,
    CardProvider,
    CardStatus,
    ProviderError,
    Transaction,
    TxnStatus,
)


class FaultMode(enum.StrEnum):
    FAIL_BEFORE = "fail_before"  # raise; the side effect never happens
    FAIL_AFTER = "fail_after"  # side effect happens, then raise
    HANG_BEFORE = "hang_before"  # block until cancelled; no side effect
    HANG_AFTER = "hang_after"  # side effect happens, then block until cancelled


class InjectedFault(ProviderError):
    pass


class EventType(enum.StrEnum):
    AUTHORIZATION = "authorization"
    DECLINE = "decline"
    SETTLEMENT = "settlement"
    VOID = "void"


@dataclass(frozen=True)
class NetworkEvent:
    id: int
    type: EventType
    transaction: Transaction
    at: datetime


def _normalize_merchant(merchant: str) -> str:
    return merchant.strip().casefold()


class MockCardProvider(CardProvider):
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cards: dict[str, Card] = {}
        self._txns: dict[str, Transaction] = {}
        self._outbox: list[NetworkEvent] = []
        self._ids = itertools.count(1)
        self._faults: dict[tuple[str, int], FaultMode] = {}
        self.calls: Counter[str] = Counter()

    def inject(self, method: str, mode: FaultMode, *, on_call: int | None = None) -> None:
        """Fail the `on_call`-th call to `method` (1-based; default: the next call)."""
        if not callable(getattr(CardProvider, method, None)):
            raise ValueError(f"not a CardProvider method: {method}")
        self._faults[(method, on_call or self.calls[method] + 1)] = mode

    async def _run(self, method: str, effect: Callable[[], Any]) -> Any:
        self.calls[method] += 1
        mode = self._faults.pop((method, self.calls[method]), None)
        if mode is FaultMode.FAIL_BEFORE:
            raise InjectedFault(f"{method}: failed before effect")
        if mode is FaultMode.HANG_BEFORE:
            await asyncio.Event().wait()
        result = effect()
        if mode is FaultMode.FAIL_AFTER:
            raise InjectedFault(f"{method}: failed after effect")
        if mode is FaultMode.HANG_AFTER:
            await asyncio.Event().wait()
        return result

    async def create_card(self, idem_key: str, merchant_lock: str, limit: Decimal) -> Card:
        def effect() -> Card:
            card = Card(
                token=f"card_{uuid.uuid4().hex}",
                idem_key=idem_key,
                merchant_lock=merchant_lock,
                limit=to_money(limit),
                status=CardStatus.OPEN,
                created_at=self._clock(),
            )
            self._cards[card.token] = card
            return card

        return await self._run("create_card", effect)

    async def find_card_by_key(self, idem_key: str) -> Card | None:
        return await self._run(
            "find_card_by_key",
            lambda: next((c for c in self._cards.values() if c.idem_key == idem_key), None),
        )

    async def close_card(self, token: str) -> Card:
        return await self._run("close_card", lambda: self._set_status(token, CardStatus.CLOSED))

    async def pause_card(self, token: str) -> Card:
        return await self._run("pause_card", lambda: self._set_status(token, CardStatus.PAUSED))

    async def list_cards(self) -> list[Card]:
        return await self._run("list_cards", lambda: list(self._cards.values()))

    async def list_transactions(self, since: datetime | None = None) -> list[Transaction]:
        return await self._run(
            "list_transactions",
            lambda: [t for t in self._txns.values() if since is None or t.updated_at >= since],
        )

    def _set_status(self, token: str, status: CardStatus) -> Card:
        card = self._get_card(token)
        if card.status is not CardStatus.CLOSED:  # closing is permanent
            card = self._cards[token] = replace(card, status=status)
        return card

    def _get_card(self, token: str) -> Card:
        try:
            return self._cards[token]
        except KeyError:
            raise CardNotFound(token) from None

    def authorize(self, card_token: str, merchant: str, amount: Decimal | int | str) -> Transaction:
        card = self._get_card(card_token)
        amount = to_money(amount)
        if card.status is not CardStatus.OPEN:
            reason = f"card_{card.status}"
        elif _normalize_merchant(merchant) != _normalize_merchant(card.merchant_lock):
            reason = "merchant_mismatch"
        elif self._committed(card_token) + amount > card.limit:
            reason = "over_limit"
        else:
            reason = None
        now = self._clock()
        txn = Transaction(
            token=f"txn_{uuid.uuid4().hex}",
            card_token=card_token,
            merchant=merchant,
            amount=amount,
            status=TxnStatus.DECLINED if reason else TxnStatus.AUTHORIZED,
            settled_amount=None,
            decline_reason=reason,
            created_at=now,
            updated_at=now,
        )
        self._txns[txn.token] = txn
        self._emit(EventType.DECLINE if reason else EventType.AUTHORIZATION, txn)
        return txn

    def settle(self, txn_token: str, amount: Decimal | int | str | None = None) -> Transaction:
        """Settle an authorization, possibly for a different amount. Works on a
        closed card: closing stops new authorizations, not settlement of old ones."""
        txn = self._authorized(txn_token)
        settled = to_money(amount) if amount is not None else txn.amount
        txn = self._txns[txn_token] = replace(
            txn, status=TxnStatus.SETTLED, settled_amount=settled, updated_at=self._clock()
        )
        self._emit(EventType.SETTLEMENT, txn)
        return txn

    def void(self, txn_token: str) -> Transaction:
        txn = self._authorized(txn_token)
        txn = self._txns[txn_token] = replace(txn, status=TxnStatus.VOIDED, updated_at=self._clock())
        self._emit(EventType.VOID, txn)
        return txn

    def take_events(
        self, *, limit: int | None = None, order: str = "fifo", seed: int | None = None
    ) -> list[NetworkEvent]:
        """Deliver pending events. `limit` leaves the rest pending (late delivery);
        `order` is "fifo", "reversed", or "shuffled" (out-of-order delivery)."""
        events = list(self._outbox)
        if order == "reversed":
            events.reverse()
        elif order == "shuffled":
            random.Random(seed).shuffle(events)
        elif order != "fifo":
            raise ValueError(f"unknown order: {order}")
        taken = events[:limit] if limit is not None else events
        taken_ids = {e.id for e in taken}
        self._outbox = [e for e in self._outbox if e.id not in taken_ids]
        return taken

    def _authorized(self, txn_token: str) -> Transaction:
        txn = self._txns[txn_token]
        if txn.status is not TxnStatus.AUTHORIZED:
            raise ValueError(f"{txn_token} is {txn.status}, not authorized")
        return txn

    def _committed(self, card_token: str) -> Decimal:
        total = Decimal("0")
        for t in self._txns.values():
            if t.card_token != card_token:
                continue
            if t.status is TxnStatus.AUTHORIZED:
                total += t.amount
            elif t.status is TxnStatus.SETTLED:
                total += t.settled_amount
        return total

    def _emit(self, type_: EventType, txn: Transaction) -> None:
        self._outbox.append(NetworkEvent(next(self._ids), type_, txn, self._clock()))
