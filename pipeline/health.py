"""
Per-source health checks, shared by the CLI test scripts (auth/*.py) and the API's
GET /api/status and the pipeline runner. Each returns a plain result instead of printing
and exiting, so callers (API, scheduler) can handle failure without a crashed process.
"""
import base64
import os
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

# What Phase 2's collectors will need. A valid token is not the same thing as a useful one:
# Slack's auth.test says "ok" for any live xoxp regardless of its scope set, and a Zoom app whose
# creator lacked the role to grant an admin scope still mints tokens. Check the grants here so a
# misconfigured app fails in Phase 1 rather than on the first collector call.
SLACK_SCOPES = {
    "channels:history", "groups:history", "im:history", "mpim:history",
    "channels:read", "groups:read", "im:read", "mpim:read",
    "users:read",
}
# Dropped when SLACK_SKIP_DMS is set — the digest can be built without reading personal DMs.
SLACK_DM_SCOPES = {"im:history", "im:read"}
# cloud_recording:read:meeting_transcript:admin stays out: transcripts are optional.
ZOOM_SCOPES = {
    "meeting:read:list_summaries:admin",
    "meeting:read:summary:admin",
    "meeting:read:past_meeting:admin",
    "meeting:read:list_past_participants:admin",
    "report:read:user:admin",
}
# gmail.readonly is the narrowest scope that reads bodies and supports `q` search: gmail.metadata is
# also Restricted and can do neither (research 2026-09-20 §3). Google reports granted scopes as full
# URLs in the token response, so the constant is the URL, not the short name.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def _missing(required: set[str], granted: set[str]) -> str:
    """'' when every required scope is granted, else a stable, comma-separated list."""
    return ", ".join(sorted(required - granted))


@dataclass
class HealthResult:
    source: str
    status: str  # "ok" | "error"
    detail: str


def _env() -> dict:
    """
    Config from .env, with the real environment taking precedence.

    Both are needed. Local dev reads the file; the container has no .env at all — the Dockerfile
    doesn't copy it and compose's `env_file:` injects it into the process environment instead — so
    reading only the file made every tenant and workspace invisible once deployed.
    """
    merged = dict(dotenv_values(ROOT / ".env"))
    for key, value in os.environ.items():
        if key.startswith(("M365_", "SLACK_", "ZOOM_", "MSTODO_", "GMAIL_")) or key in _SINGLE_KEYS:
            merged[key] = value
    return merged


# Non-prefixed keys worth picking up from the environment. Deliberately a fixed list rather than
# merging all of os.environ, which would pull in hundreds of unrelated container variables.
_SINGLE_KEYS = {"RMAPI_CONFIG", "LAN_HOST", "TZ"}


def env_flag(key: str) -> bool:
    """
    A boolean .env setting, parsed rather than tested for emptiness.

    SLACK_SKIP_DMS=false must mean false. Bare truthiness would read it as "skip DMs" and silently
    drop every DM from the digest while the health check stayed green.
    """
    value = (_env().get(key) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


_M365_ALIAS_KEY = re.compile(r"^M365_ORG(\d+)_ALIAS$")
_SLACK_TOKEN_KEY = re.compile(r"^SLACK_([A-Z0-9]+)_TOKEN$")
# Anchored on the _REFRESH_TOKEN suffix so the shared GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET are not
# mistaken for an account labeled CLIENT.
_GMAIL_TOKEN_KEY = re.compile(r"^GMAIL_([A-Z0-9]+)_REFRESH_TOKEN$")


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


def gmail_accounts() -> dict[str, Optional[str]]:
    """{label: refresh_token} for every GMAIL_<LABEL>_REFRESH_TOKEN in .env; label is lowercased."""
    env = _env()
    out: dict[str, Optional[str]] = {}
    for key, value in env.items():
        m = _GMAIL_TOKEN_KEY.match(key)
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


def zoom_token_response() -> dict:
    """
    Fetch a Zoom server-to-server token. Raises on any failure.

    Separate from check_zoom so the collector can get a live token without re-implementing the
    account_credentials dance. Tokens last an hour with no refresh, so every caller fetches its own.
    """
    env = _env()
    account_id = env.get("ZOOM_ACCOUNT_ID")
    client_id = env.get("ZOOM_CLIENT_ID")
    client_secret = env.get("ZOOM_CLIENT_SECRET")
    if not all([account_id, client_id, client_secret]):
        raise RuntimeError("Missing Zoom credentials in .env")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://zoom.us/oauth/token",
        headers={"Authorization": f"Basic {basic}"},
        params={"grant_type": "account_credentials", "account_id": account_id},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def zoom_token() -> str:
    """Just the access token, for collectors."""
    return zoom_token_response()["access_token"]


def check_zoom() -> HealthResult:
    try:
        data = zoom_token_response()
    except requests.RequestException as exc:
        return HealthResult("zoom", "error", str(exc))
    except RuntimeError as exc:
        return HealthResult("zoom", "error", str(exc))

    granted = set((data.get("scope") or "").split())
    missing = _missing(ZOOM_SCOPES, granted)
    if missing:
        # Usually the app creator's role couldn't grant the admin scopes — recreate as account owner.
        return HealthResult("zoom", "error", f"token valid but missing scopes: {missing}")

    return HealthResult("zoom", "ok", f"token valid, expires_in={data['expires_in']}s, {len(granted)} scopes")


class GmailAuthError(Exception):
    """
    The Gmail refresh token is dead. Hard failure: retrying cannot help, only a new bootstrap can.

    Kept apart from requests.RequestException so callers can tell "re-authorize" from "Google is
    having a moment" without parsing message text.
    """


def gmail_token_response(label: str) -> dict:
    """
    Exchange the account's refresh token for an access token. Returns the raw token JSON.

    Unlike MSAL, Google returns no new refresh token on refresh, so there is no cache to write
    back and every caller simply fetches its own access token (they last an hour).

    Raises GmailAuthError on invalid_grant, RuntimeError when the config is incomplete, and
    requests.RequestException (incl. HTTPError) for every other failure.
    """
    env = _env()
    client_id = env.get("GMAIL_CLIENT_ID")
    client_secret = env.get("GMAIL_CLIENT_SECRET")
    if not all([client_id, client_secret]):
        raise RuntimeError("Missing GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env")
    refresh_token = gmail_accounts().get(label.lower())
    if not refresh_token:
        raise RuntimeError(f"No GMAIL_{label.upper()}_REFRESH_TOKEN in .env — run auth/gmail_bootstrap.py {label}")

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    if resp.status_code == 400:
        # Only invalid_grant is a dead token; other 400s (invalid_client, a bad request) fall
        # through to raise_for_status so they are not misreported as "re-authorize".
        try:
            error = resp.json().get("error")
        except ValueError:
            error = None
        if error == "invalid_grant":
            raise GmailAuthError(
                f"Refresh token rejected (invalid_grant) for {label}. Likely causes: consent screen "
                "still in Testing (tokens expire after 7 days), Google account password changed, or "
                f"access revoked. Re-authorize with: auth/gmail_bootstrap.py {label}"
            )
    resp.raise_for_status()
    return resp.json()


def check_gmail(label: str) -> HealthResult:
    source = f"gmail_{label}"
    try:
        data = gmail_token_response(label)
    except GmailAuthError as exc:
        return HealthResult(source, "error", str(exc))
    except RuntimeError as exc:
        return HealthResult(source, "error", str(exc))
    except requests.RequestException as exc:
        return HealthResult(source, "error", str(exc))

    granted = set((data.get("scope") or "").split())
    if GMAIL_SCOPE not in granted:
        return HealthResult(source, "error", f"token valid but missing scope: {GMAIL_SCOPE} — re-run auth/gmail_bootstrap.py {label}")

    # getProfile costs 1 quota unit and accepts gmail.readonly: the cheapest proof the mailbox is reachable.
    try:
        resp = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {data['access_token']}"},
            timeout=15,
        )
        resp.raise_for_status()
        profile = resp.json()
    except requests.RequestException as exc:
        return HealthResult(source, "error", str(exc))

    return HealthResult(source, "ok", f"account={profile.get('emailAddress')}, {len(granted)} scopes")


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
        # Slack returns the granted user-token scopes in a response header on any Web API call,
        # so this needs no extra request and no scope of its own.
        granted = set(h.strip() for h in resp.headers.get("x-oauth-scopes", "").split(",") if h.strip())
    except requests.RequestException as exc:
        return HealthResult(source, "error", str(exc))

    if not data.get("ok"):
        return HealthResult(source, "error", data.get("error", "unknown error"))

    required = SLACK_SCOPES - (SLACK_DM_SCOPES if env_flag("SLACK_SKIP_DMS") else set())
    missing = _missing(required, granted)
    if missing:
        return HealthResult(source, "error", f"token valid but missing scopes: {missing} — reinstall the app")

    return HealthResult(source, "ok", f"user={data['user']} team={data['team']}, {len(granted)} scopes")


def known_sources() -> list[str]:
    """Every source id the current .env declares: m365_<alias>, zoom, slack_<label>, gmail_<label>."""
    return (
        [f"m365_{a}" for a in m365_aliases()]
        + ["zoom"]
        + [f"slack_{l}" for l in slack_workspaces()]
        + [f"gmail_{l}" for l in gmail_accounts()]
    )


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

    for label in gmail_accounts():
        if active_sources.get(f"gmail_{label}", False):
            results.append(check_gmail(label))

    return results
