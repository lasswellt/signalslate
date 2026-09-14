from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import config, runs, status
from api.scheduler import start_scheduler
from pipeline.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield


app = FastAPI(title="SignalSlate API", lifespan=lifespan)

# LAN-only deployment — Nuxt dev server and the built frontend both need this.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(status.router, prefix="/api")
app.include_router(runs.router, prefix="/api")
app.include_router(config.router, prefix="/api")


@app.get("/api/health")
def api_health():
    return {"ok": True}
