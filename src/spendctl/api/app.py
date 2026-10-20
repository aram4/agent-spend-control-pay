from fastapi import FastAPI, Response, status

from spendctl.db.engine import ping_db

app = FastAPI(title="spendctl")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
async def ready(response: Response) -> dict:
    try:
        await ping_db()
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "db": "error"}
    return {"status": "ok", "db": "ok"}
