"""
Per-source health checks, shared by the CLI test scripts (auth/*.py) and the API's
GET /api/status and the pipeline runner. Each returns a plain result instead of printing
and exiting, so callers (API, scheduler) can handle failure without a crashed process.
"""
import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import msal
import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
TOKEN_DIR = ROOT / "tokens"
# Delegated Graph scopes. Decide the full list up front: adding one later forces re-consent in
# every tenant. MSAL adds offline_access/openid/profile itself — do not list them here.
SCOPES = ["Mail.Read", "Calendars.Read", "Tasks.Read", "Chat.Read", "User.Read"]


@dataclass
class HealthResult:
    source: str
    status: str  # "ok" | "error"
    detail: str


def _env() -> dict:
    return dotenv_values(ROOT / ".env")


_M365_ALIAS_KEY = re.compile(r"^M365_ORG(\d+)_ALIAS$")
_SLACK_TOKEN_KEY = re.compile(r"^SLACK_([A-Z0-9]+)_TOKEN$")


def m365_aliases() -> list[str]:
    """Every alias declared as M365_ORG<n>_ALIAS in .env, in numeric order. Any number of tenants."""
    env = _env()
    found = []
    for key, value in env.items():
        m = _M365_ALIAS_KEY.match(key)
        if m and value:
            found.append((int(m.group(1)), value))
    return [alias for _, alias in sorted(found)]


def slack_workspaces() -> dict[str, Optional[str]]:
    """{label: token} for every SLACK_<LABEL>_TOKEN in .env; label is lowercased. Any number."""
    env = _env()
    out: dict[str, Optional[str]] = {}
    for key, value in env.items():
        m = _SLACK_TOKEN_KEY.match(key)
        if m:
            out[m.group(1).lower()] = value or None
    return dict(sorted(out.items()))


def m365_tenant_config(alias: str) -> Optional[dict]:
    """
    Resolve alias -> {tenant_id, client_id}. One shared multi-tenant app (M365_CLIENT_ID) is the
    default; a per-tenant M365_ORG<n>_CLIENT_ID overrides it for a tenant that insisted on its own
    registration. Returns None if the alias isn't in .env or is missing an id.
    """
    env = _env()
    for key, value in env.items():
        m = _M365_ALIAS_KEY.match(key)
        if m and value == alias:
            prefix = f"M365_ORG{m.group(1)}"
            tenant_id = env.get(f"{prefix}_TENANT_ID")
            client_id = env.get(f"{prefix}_CLIENT_ID") or env.get("M365_CLIENT_ID")
            if not tenant_id or not client_id:
                return None
            return {"alias": alias, "tenant_id": tenant_id, "client_id": client_id}
    return None


def m365_cache_path(alias: str) -> Path:
    return TOKEN_DIR / f"{alias}_cache.bin"


def m365_app(cfg: dict, cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    # Tenant-specific authority on purpose: /common caches under the real tenant and then misses.
    return msal.PublicClientApplication(
        client_id=cfg["client_id"],
        authority=f"https://login.microsoftonline.com/{cfg['tenant_id']}",
        token_cache=cache,
    )


def check_m365(alias: str) -> HealthResult:
    source = f"m365_{alias}"
    cfg = m365_tenant_config(alias)
    if cfg is None:
        return HealthResult(source, "error", f"No usable .env entry for alias={alias!r} (tenant_id + client_id)")

    cache_path = m365_cache_path(alias)
    if not cache_path.exists():
        return HealthResult(source, "error", "No token cache — run auth/m365_bootstrap.py once on a machine with a browser")

    cache = msal.SerializableTokenCache()
    cache.deserialize(cache_path.read_text())
    app = m365_app(cfg, cache)
    accounts = app.get_accounts()
    if not accounts:
        return HealthResult(source, "error", "Cache has no account — re-run auth/m365_bootstrap.py")

    # _with_error distinguishes "nothing cached" (None) from "refresh rejected" (error dict), so
    # a revoked refresh token surfaces its AADSTS code on the dashboard instead of a generic message.
    result = app.acquire_token_silent_with_error(SCOPES, account=accounts[0])
    if cache.has_state_changed:
        cache_path.write_text(cache.serialize())

    if not result:
        return HealthResult(source, "error", "No token in cache for these scopes — re-run auth/m365_bootstrap.py")
    if "access_token" not in result:
        err = result.get("error_description") or result.get("error") or "silent refresh failed"
        return HealthResult(source, "error", err)

    return HealthResult(source, "ok", f"token valid, expires_in={result['expires_in']}s")


def check_zoom() -> HealthResult:
    env = _env()
    account_id = env.get("ZOOM_ACCOUNT_ID")
    client_id = env.get("ZOOM_CLIENT_ID")
    client_secret = env.get("ZOOM_CLIENT_SECRET")
    if not all([account_id, client_id, client_secret]):
        return HealthResult("zoom", "error", "Missing Zoom credentials in .env")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = requests.post(
            "https://zoom.us/oauth/token",
            headers={"Authorization": f"Basic {basic}"},
            params={"grant_type": "account_credentials", "account_id": account_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return HealthResult("zoom", "error", str(exc))

    return HealthResult("zoom", "ok", f"token valid, expires_in={data['expires_in']}s")


def check_slack(label: str, token: Optional[str]) -> HealthResult:
    source = f"slack_{label}"
    if not token:
        return HealthResult(source, "error", f"No token in .env for {label}")
    try:
        resp = requests.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        data = resp.json()
    except requests.RequestException as exc:
        return HealthResult(source, "error", str(exc))

    if not data.get("ok"):
        return HealthResult(source, "error", data.get("error", "unknown error"))

    return HealthResult(source, "ok", f"user={data['user']} team={data['team']}")


def known_sources() -> list[str]:
    """Every source id the current .env declares: m365_<alias>, zoom, slack_<label>."""
    return [f"m365_{a}" for a in m365_aliases()] + ["zoom"] + [f"slack_{l}" for l in slack_workspaces()]


def check_all_configured(active_sources: dict[str, bool]) -> list[HealthResult]:
    """Runs whichever declared sources are toggled on in config; skips the rest silently."""
    results: list[HealthResult] = []

    for alias in m365_aliases():
        if active_sources.get(f"m365_{alias}", False):
            results.append(check_m365(alias))

    if active_sources.get("zoom", False):
        results.append(check_zoom())

    for label, token in slack_workspaces().items():
        if active_sources.get(f"slack_{label}", False):
            results.append(check_slack(label, token))

    return results
