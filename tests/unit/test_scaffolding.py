import uuid

import httpx
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from spendctl.api.app import app
from spendctl.config import get_settings
from spendctl.db.engine import ping_db
from spendctl.worker import ACTIVITIES, build_worker, ping


async def test_settings_target_test_db_and_privacy_off():
    settings = get_settings()
    assert settings.database_url.endswith("/spendctl_test")
    assert settings.privacy_live is False


async def test_db_reachable():
    await ping_db()


async def test_health_and_ready():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    assert ready.status_code == 200 and ready.json()["db"] == "ok"


@workflow.defn(sandboxed=False)
class _PingWorkflow:
    @workflow.run
    async def run(self) -> str:
        from datetime import timedelta

        return await workflow.execute_activity(ping, start_to_close_timeout=timedelta(seconds=5))


async def test_worker_runs_ping_activity():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"test-{uuid.uuid4()}"
        # The real worker (activities only) serves the activity; a test-only worker runs the workflow.
        async with build_worker(env.client, task_queue=queue), Worker(
            env.client, task_queue=queue, workflows=[_PingWorkflow]
        ):
            result = await env.client.execute_workflow(
                _PingWorkflow.run, id=f"ping-{uuid.uuid4()}", task_queue=queue
            )
    assert result == "pong"
    assert ACTIVITIES == [ping]
