import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


class CardStatus(enum.StrEnum):
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"


class TxnStatus(enum.StrEnum):
    AUTHORIZED = "authorized"
    DECLINED = "declined"
    SETTLED = "settled"
    VOIDED = "voided"


@dataclass(frozen=True)
class Card:
    token: str
    idem_key: str
    merchant_lock: str
    limit: Decimal
    status: CardStatus
    created_at: datetime


@dataclass(frozen=True)
class Transaction:
    token: str
    card_token: str
    merchant: str
    amount: Decimal
    status: TxnStatus
    settled_amount: Decimal | None
    decline_reason: str | None
    created_at: datetime
    updated_at: datetime


class ProviderError(Exception):
    """Transient provider failure; safe to retry."""


class CardNotFound(Exception):
    pass


class CardProvider(ABC):
    @abstractmethod
    async def create_card(self, idem_key: str, merchant_lock: str, limit: Decimal) -> Card: ...

    @abstractmethod
    async def find_card_by_key(self, idem_key: str) -> Card | None: ...

    @abstractmethod
    async def close_card(self, token: str) -> Card: ...

    @abstractmethod
    async def pause_card(self, token: str) -> Card: ...

    @abstractmethod
    async def list_cards(self) -> list[Card]: ...

    @abstractmethod
    async def list_transactions(self, since: datetime | None = None) -> list[Transaction]: ...
