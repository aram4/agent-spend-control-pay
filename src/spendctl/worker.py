import asyncio

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from spendctl.config import get_settings


@activity.defn
async def ping() -> str:
    """No-op activity; the SDK refuses to start a worker with nothing registered."""
    return "pong"


WORKFLOWS: list = []
ACTIVITIES: list = [ping]


def build_worker(client: Client, task_queue: str | None = None) -> Worker:
    return Worker(
        client,
        task_queue=task_queue or get_settings().task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
    )


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await build_worker(client).run()


if __name__ == "__main__":
    asyncio.run(main())
