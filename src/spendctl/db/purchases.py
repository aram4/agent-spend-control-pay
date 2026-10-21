import uuid
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from spendctl.db.engine import get_sessionmaker
from spendctl.db.models import PURCHASE_STATES, Purchase


def workflow_id_for(purchase_id: uuid.UUID) -> str:
    return f"purchase-{purchase_id}"


@dataclass(frozen=True)
class PurchaseRef:
    id: uuid.UUID
    workflow_id: str


async def create_purchase(*, sessionmaker: async_sessionmaker | None = None) -> PurchaseRef:
    sm = sessionmaker or get_sessionmaker()
    purchase_id = uuid.uuid4()
    async with sm() as session, session.begin():
        session.add(Purchase(id=purchase_id, workflow_id=workflow_id_for(purchase_id)))
    return PurchaseRef(purchase_id, workflow_id_for(purchase_id))


async def set_state(
    purchase_id: uuid.UUID, state: str, *, sessionmaker: async_sessionmaker | None = None
) -> None:
    # Unvalidated until the Phase 4 state machine replaces it.
    if state not in PURCHASE_STATES:
        raise ValueError(state)
    sm = sessionmaker or get_sessionmaker()
    async with sm() as session, session.begin():
        await session.execute(update(Purchase).where(Purchase.id == purchase_id).values(state=state))
