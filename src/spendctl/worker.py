import asyncio

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from spendctl.config import get_settings
from spendctl.providers.base import CardProvider
from spendctl.providers.mock import MockCardProvider
from spendctl.workflows.activities import all_activities
from spendctl.workflows.purchase import PurchaseWorkflow


@activity.defn
async def ping() -> str:
    """No-op activity for checking that a worker is up."""
    return "pong"


WORKFLOWS: list = [PurchaseWorkflow]


def default_provider() -> CardProvider:
    if get_settings().privacy_live:
        raise NotImplementedError("PrivacyCardProvider is not built yet; unset PRIVACY_LIVE")
    return MockCardProvider()


def build_worker(
    client: Client, task_queue: str | None = None, provider: CardProvider | None = None
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue or get_settings().task_queue,
        workflows=WORKFLOWS,
        activities=[ping, *all_activities(provider or default_provider())],
    )


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await build_worker(client).run()


if __name__ == "__main__":
    asyncio.run(main())
