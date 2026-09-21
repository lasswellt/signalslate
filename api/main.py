from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routers import config, runs, status
from api.scheduler import start_scheduler
from api.security import install_security
from pipeline import health
from pipeline.db import init_db, reap_orphaned_runs


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Before anything can be blocked by it: clear any run stranded by a previous process death.
    reaped = reap_orphaned_runs()
    if reaped:
        print(f"[signalslate] marked {reaped} interrupted run(s) as failed")
    start_scheduler()
    yield


app = FastAPI(title="SignalSlate API", lifespan=lifespan)

install_security(app, allowed_origins=health.web_origins())

app.include_router(status.router, prefix="/api")
app.include_router(runs.router, prefix="/api")
app.include_router(config.router, prefix="/api")


@app.get("/api/health")
def api_health():
    return {"ok": True}
