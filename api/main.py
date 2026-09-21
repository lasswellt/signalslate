import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI

from api.routers import collectors, config, connections as connections_router, oauth, runs, status
from api.scheduler import start_scheduler
from api.security import install_host_guard, install_security, normalize_host, normalize_origin
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


def _configured_allowed_hosts() -> list[str]:
    """
    ALLOWED_HOSTS split on commas, blanks dropped.

    The real environment is read first: ALLOWED_HOSTS is not one of the keys health merges out of
    os.environ (that list is fixed), and in the container there is no .env file, so compose's
    env_file would otherwise never reach it. The .env file is the fallback for local runs.
    """
    raw = os.environ["ALLOWED_HOSTS"] if "ALLOWED_HOSTS" in os.environ else health.env().get("ALLOWED_HOSTS")
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def allowed_hosts() -> list[str]:
    """
    Hostnames the API may be reached by, for the Host guard.

    The browser calls the API at LAN_HOST:8000 (compose sets NUXT_PUBLIC_API_BASE from it) and the web
    container makes no server-side call to it by service name, so no service name is listed: an
    operator whose proxy forwards a different Host adds it to ALLOWED_HOSTS. Wildcards never widen
    the list; install_host_guard drops them.
    """
    hosts = ["localhost", "127.0.0.1", "::1"]
    for origin in health.web_origins():
        normalized = normalize_origin(origin)
        if normalized is not None and (hostname := urlsplit(normalized).hostname):
            hosts.append(hostname)
    public_base = health.public_base_url()
    if public_base is not None and (hostname := urlsplit(public_base).hostname):
        hosts.append(hostname)
    lan_host = (health.env().get("LAN_HOST") or "").strip()
    if lan_host:
        hosts.append(lan_host)
    return hosts + _configured_allowed_hosts()


app = FastAPI(title="SignalSlate API", lifespan=lifespan)

install_security(app, allowed_origins=health.web_origins())
# After install_security: the last middleware added is the outermost, so an unlisted Host is refused
# before CORS or the write guard sees the request.
install_host_guard(app, allowed_hosts=allowed_hosts())
if unusable := [entry for entry in _configured_allowed_hosts() if normalize_host(entry) is None]:
    print(f"[signalslate] ignoring {len(unusable)} unusable ALLOWED_HOSTS entries (wildcards are never allowed)")

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
