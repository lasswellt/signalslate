"""
Single JSON file as the source of truth for user-editable settings. Small enough that a file
beats a DB table here — easy to hand-edit or back up, and the web UI is the only writer.

active_sources is keyed by whatever sources .env declares (see pipeline.health.known_sources), so
adding a tenant or workspace to .env makes it appear in the config UI, defaulting to on.
"""
import json
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "data" / "config.json"

STATIC_DEFAULTS = {
    "schedule_cron": "0 6 * * *",  # 6:00 AM daily
    "tracker": "mstodo",  # mstodo | todoist | none
}

# Registrar connections (namecheap/godaddy/wordpress) are not collector sources, so they have no
# active_sources entry. Their on/off lives in connection_active instead; an id missing there is on.

_lock = threading.Lock()


def _defaults() -> dict[str, Any]:
    from pipeline.health import known_sources  # local import: health imports nothing from here

    return {**STATIC_DEFAULTS, "active_sources": {s: True for s in known_sources()}, "connection_active": {}}


def _merge(stored: dict[str, Any]) -> dict[str, Any]:
    """Stored values win; sources newly declared in .env get added (on); undeclared ones dropped."""
    defaults = _defaults()
    merged = {**defaults, **stored}
    stored_sources = stored.get("active_sources", {})
    merged["active_sources"] = {
        s: stored_sources.get(s, True) for s in defaults["active_sources"]
    }
    stored_conn = stored.get("connection_active")
    merged["connection_active"] = (
        {str(k): bool(v) for k, v in stored_conn.items()} if isinstance(stored_conn, dict) else {}
    )
    return merged


def load_config() -> dict[str, Any]:
    with _lock:
        if not CONFIG_PATH.exists():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            cfg = _defaults()
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
            return cfg
        return _merge(json.loads(CONFIG_PATH.read_text()))


def save_config(new_config: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        merged = _merge(new_config)
        CONFIG_PATH.write_text(json.dumps(merged, indent=2))
        return merged


def forget_source(source_id: str) -> None:
    """
    Drop a source's stored toggle so a connection re-created under the same id starts on rather than
    inheriting the old value.

    Works on the raw file, never _merge: _merge calls known_sources(), which reaches the connection
    store, and this is called while a connection is being deleted. pipeline.connections must never be
    called from here, or a caller holding the connection lock deadlocks against load_config, which
    holds _lock while known_sources() runs.
    """
    with _lock:
        if not CONFIG_PATH.exists():
            return
        stored = json.loads(CONFIG_PATH.read_text())
        changed = False
        for key in ("active_sources", "connection_active"):
            section = stored.get(key)
            if isinstance(section, dict) and source_id in section:
                del section[source_id]
                changed = True
        if changed:
            CONFIG_PATH.write_text(json.dumps(stored, indent=2))


def set_source_active(source_id: str, active: bool) -> dict[str, Any]:
    """
    Toggle one source and return the merged config. Load and save share one _lock hold, so two
    toggles cannot lose each other's write. An id .env does not declare is ignored, like any other
    undeclared source in _merge; the caller validates it against known_sources().
    """
    with _lock:
        stored = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        merged = _merge(stored)
        if source_id in merged["active_sources"]:
            merged["active_sources"][source_id] = active
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(merged, indent=2))
        return merged
