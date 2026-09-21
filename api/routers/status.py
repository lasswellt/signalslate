from typing import Optional

from fastapi import APIRouter

from api.scheduler import next_run_time
from api.serialize import iso_z
from pipeline import connections, health
from pipeline.db import latest_run, latest_source_health
from pipeline.redact import redact

router = APIRouter(tags=["status"])

# Free text on a run or a health row can quote a provider response, which can carry a credential.
_MAX_TEXT = 1000


def _scrub(text: Optional[str], known: list[str]) -> Optional[str]:
    if text is None:
        return None
    return redact(text, known, max_len=_MAX_TEXT)


@router.get("/status")
def get_status():
    run = latest_run()
    source_rows = latest_source_health()
    # Stored secrets are scrubbed by value as well as by shape; empty (and cheap) without a vault.
    known = connections.secret_values()

    return {
        "last_run": None
        if run is None
        else {
            "id": run.id,
            # "manual" | "scheduled" | "manual-source": a single-source run from the collectors page
            # carries the last one, so anything switching on this in the web app needs all three.
            "trigger": run.trigger,
            "status": run.status,
            "started_at": iso_z(run.started_at),
            "finished_at": iso_z(run.finished_at),
            "summary": _scrub(run.summary, known),
            "error": _scrub(run.error, known),
        },
        "next_scheduled_run": iso_z(next_run_time()),
        "source_health": [
            {
                "source": h.source,
                "status": h.status,
                "detail": _scrub(h.detail, known),
                "checked_at": iso_z(h.checked_at),
            }
            for h in source_rows
        ],
    }


@router.get("/system")
def get_system():
    """
    What the UI needs to explain itself: is there a key, is the store on, which sign-in modes work.

    The key is reported as configured-or-not and the store as active-or-not separately: a key that
    is set but unparseable leaves the app running from .env alone, and that state must be visible.
    Nothing here echoes the key or the public URL, only whether each is usable.

    unseeded_env lists the NAMES of .env keys whose declarations could not be imported into the
    store and therefore still run from .env; their values are never part of any response.
    """
    callback_ready = health.public_base_url() is not None
    # Paste-back needs no public address; callback needs an https PUBLIC_BASE_URL to register.
    modes = ["paste_back", "callback"] if callback_ready else ["paste_back"]
    return {
        "secret_key_configured": health.secret_key_setting() is not None,
        "store_active": connections.get_vault() is not None,
        "public_base_url_configured": callback_ready,
        "web_origins": health.web_origins(),
        "unseeded_env": connections.unseeded_env_keys(),
        "oauth": {"google": {"modes": list(modes)}, "microsoft": {"modes": list(modes)}},
    }
