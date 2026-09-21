from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routers import collectors, config, connections as connections_router, oauth, runs, status
from api.scheduler import start_scheduler
from api.security import install_security
from pipeline import connections, health
from pipeline.crypto import SecretKeyInvalid, Vault
from pipeline.db import init_db, reap_orphaned_runs


def _start_connection_store() -> None:
    """
    Installs the vault, imports .env once and registers the env overlay, in that order.

    The overlay is registered only when a vault exists: without a key nothing can be stored, and
    every reader must keep seeing exactly the .env it saw before the store existed. An unparseable
    key therefore degrades to that same env-only mode instead of stopping the app, because a typo in
    one setting must not take the digest down. The SecretKeyInvalid message names a key's position,
    never its text, so it is safe to print. State is reset explicitly so a second startup in the
    same process cannot inherit a vault or overlay from the first.
    """
    connections.set_vault(None)
    health.set_env_overlay_provider(None, None)
    try:
        vault = Vault.from_env_value(health.secret_key_setting())
    except SecretKeyInvalid as exc:
        print(f"[signalslate] SIGNALSLATE_SECRET_KEY is not usable ({exc}); running from .env only")
        return
    if vault is None:
        return
    connections.set_vault(vault)
    # The RAW env, not health.env(): seeding must see what .env declares, never what the store
    # already replaced. A tombstoned or edited connection is left alone (the store wins).
    seeded = connections.seed_from_env(health._raw_env())
    health.set_env_overlay_provider(connections.overlay_provider, connections.is_family_key)
    print(f"[signalslate] connection store active: seeded {len(seeded)} from .env, {len(connections.list_connections())} in store")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Before anything can be blocked by it: clear any run stranded by a previous process death.
    reaped = reap_orphaned_runs()
    if reaped:
        print(f"[signalslate] marked {reaped} interrupted run(s) as failed")
    _start_connection_store()
    start_scheduler()
    yield


app = FastAPI(title="SignalSlate API", lifespan=lifespan)

install_security(app, allowed_origins=health.web_origins())

app.include_router(status.router, prefix="/api")
app.include_router(runs.router, prefix="/api")
app.include_router(config.router, prefix="/api")
# GET /collectors/dry-run/{job_id} must stay ahead of anything shaped /collectors/{source}/...:
# routes match in registration order, and inside the collectors router it is declared first.
app.include_router(connections_router.router, prefix="/api")
app.include_router(collectors.router, prefix="/api")
app.include_router(oauth.router, prefix="/api")


@app.get("/api/health")
def api_health():
    return {"ok": True}
