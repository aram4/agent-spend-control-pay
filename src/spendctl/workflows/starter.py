import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.client import Client, WorkflowHandle

from spendctl.config import get_settings
from spendctl.db.purchases import create_purchase
from spendctl.money import to_money
from spendctl.workflows.purchase import PurchaseRequest, PurchaseWorkflow


async def start_purchase(
    client: Client,
    *,
    budget_id: uuid.UUID,
    merchant: str,
    amount: Decimal | int | str,
    task_queue: str | None = None,
    sessionmaker: async_sessionmaker | None = None,
    **timing: float,
) -> WorkflowHandle:
    """Record the purchase, then start its workflow. `timing` overrides PurchaseRequest windows."""
    purchase = await create_purchase(sessionmaker=sessionmaker)
    request = PurchaseRequest(
        purchase_id=str(purchase.id),
        budget_id=str(budget_id),
        merchant=merchant,
        amount=str(to_money(amount)),
        **timing,
    )
    return await client.start_workflow(
        PurchaseWorkflow.run,
        request,
        id=purchase.workflow_id,
        task_queue=task_queue or get_settings().task_queue,
    )
