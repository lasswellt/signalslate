"""
Per-source health checks, shared by the CLI test scripts (auth/*.py) and the API's
GET /api/status and the pipeline runner. Each returns a plain result instead of printing
and exiting, so callers (API, scheduler) can handle failure without a crashed process.
"""
import base64
import contextvars
import fcntl
import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, TypeGuard

import msal
import requests
from dotenv import dotenv_values

from pipeline import connections, db, tokencache

# pipeline.oauth_zoom is imported lazily, inside the functions that need it, never at module level:
# oauth_zoom -> oauth_gmail -> pipeline.health (for GMAIL_SCOPE) is an existing edge, and importing
# it here would close a cycle (this module would not yet have defined its own names when
# oauth_gmail tried to read one back off it).

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
# The final S2S set the collector calls, one scope per endpoint:
# - meeting:read:list_summaries:admin  -- GET /users/{userId}/meeting_summaries (enumerate)
# - meeting:read:summary:admin         -- GET /meetings/{meetingUUID}/meeting_summary (fetch body)
# - meeting:read:list_meetings:admin   -- GET /users/{userId}/meetings (list meetings)
# - meeting:read:list_past_participants:admin -- GET /past_meetings/{meetingUUID}/participants
# - report:read:user:admin             -- GET /report/users/{userId}/meetings (past meeting report)
# cloud_recording:read:meeting_transcript:admin stays out here: transcripts are optional and a
# later task adds that scope conditionally, not in this base set.
ZOOM_SCOPES = {
    "meeting:read:list_summaries:admin",
    "meeting:read:summary:admin",
    "meeting:read:list_meetings:admin",
    "meeting:read:list_past_participants:admin",
    "report:read:user:admin",
}
# The user-level equivalents for an OAuth-mode Zoom connection (a user signs in for themselves, so
# no :admin suffix). cloud_recording:read:meeting_transcript is added conditionally, same as its
# :admin counterpart above, when the connection's include_transcripts flag is on.
ZOOM_USER_SCOPES = {
    "meeting:read:list_summaries",
    "meeting:read:summary",
    "meeting:read:list_meetings",
    "meeting:read:list_past_participants",
}
ZOOM_TRANSCRIPT_SCOPE = "cloud_recording:read:meeting_transcript"
ZOOM_TRANSCRIPT_ADMIN_SCOPE = "cloud_recording:read:meeting_transcript:admin"
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


def _raw_env() -> dict:
    """
    Config from .env, with the real environment taking precedence, and NO connection-store overlay.

    Both sources are needed. Local dev reads the file; the container has no .env at all — the
    Dockerfile doesn't copy it and compose's `env_file:` injects it into the process environment
    instead — so reading only the file made every tenant and workspace invisible once deployed.
    Startup seeding reads this, not _env(): it must see what .env declares, not what the store
    already replaced.
    """
    merged = dict(dotenv_values(ROOT / ".env"))
    for key, value in os.environ.items():
        if key.startswith(("M365_", "SLACK_", "ZOOM_", "MSTODO_", "GMAIL_")) or key in _SINGLE_KEYS:
            merged[key] = value
    return merged


# Registered by startup, never imported: pipeline.connections imports the database layer and this
# module is imported by nearly everything, so a module-level import back would be a cycle. The
# provider and its family test are ONE tuple so a reader on another thread can never see a new
# provider paired with the old test.
_overlay: Optional[tuple[Callable[[], Optional[Mapping[str, str]]], Callable[[str], bool]]] = None
# Per-context, not per-process: only the run (or test-connection) that entered sees its own view.
# A thread that never entered reads None and takes the live path.
_frozen_env: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("signalslate_frozen_env", default=None)
_env_layer: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("signalslate_env_layer", default=None)


def set_env_overlay_provider(
    provider: Optional[Callable[[], Optional[Mapping[str, str]]]],
    family_test: Optional[Callable[[str], bool]],
) -> None:
    """
    Registers where the connection store's env-style keys come from, or removes it.

    provider: returns the overlay mapping, or None to leave the environment untouched (no vault).
    family_test: True for a key the store owns; those keys are DROPPED from the raw environment
    before the overlay is added, so a connection deleted in the UI cannot come back through a
    stale .env line or the container's injected environment. Passing None for either clears both.
    """
    global _overlay
    _overlay = (provider, family_test) if provider is not None and family_test is not None else None


def _live_env() -> dict:
    """The raw environment with the registered overlay applied. Identical to _raw_env() when none is."""
    merged = _raw_env()
    registered = _overlay
    if registered is None:
        return merged
    provider, family_test = registered
    overlay = provider()
    if overlay is None:
        return merged
    # The provider hands out a read-only mapping shared between threads: copy it, never mutate it.
    merged = {key: value for key, value in merged.items() if not family_test(key)}
    merged.update(dict(overlay))
    return merged


def _env() -> dict:
    """
    The config every collector and health check reads: the overlaid environment, frozen when the
    calling context holds an env_snapshot, with any env_override layered on top.

    Always a fresh dict, so a caller that edits it cannot corrupt a frozen snapshot.
    """
    frozen = _frozen_env.get()
    env = dict(frozen) if frozen is not None else _live_env()
    layer = _env_layer.get()
    if layer:
        env.update(layer)
    return env


def env() -> dict:
    """Public read of the same config for callers outside this module."""
    return _env()


@contextmanager
def env_snapshot() -> Iterator[None]:
    """
    Freezes the overlaid environment for the calling context, so every _env() inside sees one
    consistent view even if a connection is edited meanwhile (known_sources() and dispatch() must
    agree within a run). Nested use keeps the outer snapshot. Threads that did not enter are unaffected.
    """
    if _frozen_env.get() is not None:
        yield
        return
    token = _frozen_env.set(_live_env())
    try:
        yield
    finally:
        _frozen_env.reset(token)


@contextmanager
def env_override(mapping: Mapping[str, str]) -> Iterator[None]:
    """
    Layers candidate keys over the frozen or current environment for the calling context, so a
    connection can be tested with credentials that are not saved yet. Restored on exit; nests.
    """
    token = _env_layer.set({**(_env_layer.get() or {}), **mapping})
    try:
        yield
    finally:
        _env_layer.reset(token)


# Non-prefixed keys worth picking up from the environment. Deliberately a fixed list rather than
# merging all of os.environ, which would pull in hundreds of unrelated container variables.
_SINGLE_KEYS = {
    "RMAPI_CONFIG", "LAN_HOST", "TZ", "ANTHROPIC_API_KEY", "SIGNALSLATE_MAP_MODEL",
    "SIGNALSLATE_SECRET_KEY", "WEB_ORIGINS", "PUBLIC_BASE_URL",
}

# Model ids are config, not code: Anthropic announces retirements with notice, and Haiku 4.5's is
# "not sooner than October 15, 2026". Re-check the deprecations page before that date and override
# with SIGNALSLATE_MAP_MODEL rather than editing this default in a hurry.
DEFAULT_MAP_MODEL = "claude-haiku-4-5-20251001"


def llm_settings() -> dict:
    """
    The Anthropic API key and the map-stage model id, read through _env().

    Callers must pass the key to the client explicitly: the SDK only reads os.environ, and locally
    .env is never loaded into it, so relying on the SDK's own lookup works in the container and
    silently finds nothing on a dev machine. A blank value counts as unset so `ANTHROPIC_API_KEY=`
    in .env.example (the documented placeholder) yields None, not an empty string the SDK would send.
    """
    env = _env()
    api_key = (env.get("ANTHROPIC_API_KEY") or "").strip() or None
    map_model = (env.get("SIGNALSLATE_MAP_MODEL") or "").strip() or DEFAULT_MAP_MODEL
    return {"api_key": api_key, "map_model": map_model}


def secret_key_setting() -> Optional[str]:
    """SIGNALSLATE_SECRET_KEY as written (a comma-separated key list), None when unset or blank."""
    return (_env().get("SIGNALSLATE_SECRET_KEY") or "").strip() or None


def web_origins() -> list[str]:
    """
    Origins allowed to make mutating requests: WEB_ORIGINS split on commas, blanks dropped.

    Unset (or all blank) defaults to the dev frontend, plus the same port on LAN_HOST when that is
    set, since that is the address the UI is opened at from another machine on the network.
    """
    env = _env()
    origins = [part.strip() for part in (env.get("WEB_ORIGINS") or "").split(",") if part.strip()]
    if origins:
        return origins
    origins = ["http://localhost:3000"]
    lan_host = (env.get("LAN_HOST") or "").strip()
    if lan_host:
        origins.append(f"http://{lan_host}:3000")
    return origins


def public_base_url() -> Optional[str]:
    """
    PUBLIC_BASE_URL without a trailing slash, or None when unset or not https. It comes from config
    only, never from request headers: OAuth redirect URIs built from a Host header are spoofable.
    """
    url = (_env().get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return url if url.lower().startswith("https://") else None


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
    # TOKEN_DIR is passed explicitly: tokencache has its own TOKEN_DIR, and callers (tests, the
    # bootstrap scripts) repoint this module's, so the default would read the real tokens/ directory.
    return tokencache.cache_path(alias, token_dir=TOKEN_DIR)


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

    # An alias is a filename component; one tokencache refuses (traversal, empty, too long) must become
    # this source's error, not a ValueError that aborts check_all_configured for every other source.
    try:
        tokencache.cache_path(alias, token_dir=TOKEN_DIR)
    except ValueError:
        return HealthResult(source, "error", "Alias cannot name a token cache file: use 1-64 letters, digits, '_', '.' or '-', starting with a letter or digit")

    # The lock spans load -> refresh -> save: MSAL rotates the refresh token on use, so two
    # unserialized refreshes of one alias strand the loser's token.
    with tokencache.locked(alias, token_dir=TOKEN_DIR):
        cache = tokencache.load(alias, token_dir=TOKEN_DIR)
        if cache is None:
            if not m365_cache_path(alias).exists():
                return HealthResult(source, "error", "No token cache — run auth/m365_bootstrap.py once on a machine with a browser")
            return HealthResult(source, "error", "Token cache unreadable: re-run sign-in")

        app = m365_app(cfg, cache)
        accounts = app.get_accounts()
        if not accounts:
            return HealthResult(source, "error", "Cache has no account — re-run auth/m365_bootstrap.py")

        # _with_error distinguishes "nothing cached" (None) from "refresh rejected" (error dict), so
        # a revoked refresh token surfaces its AADSTS code on the dashboard instead of a generic message.
        result = app.acquire_token_silent_with_error(SCOPES, account=accounts[0])
        tokencache.save(alias, cache, token_dir=TOKEN_DIR)

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


class ZoomAuthError(Exception):
    """
    An OAuth-mode Zoom connection cannot yield a token without a fresh sign-in: either nothing was
    ever stored, or Zoom rejected the stored refresh token (invalid_grant). Hard failure, like
    GmailAuthError above: retrying cannot help, only Sign in can. Every message here is fixed text
    with no token, secret or connection field embedded.
    """


_ZOOM_NOT_SIGNED_IN = "Zoom is not signed in — use Sign in on the Connections page"
_ZOOM_SIGN_IN_EXPIRED = "Zoom sign-in expired or was revoked — sign in again"

# Cross-process lock beside the connection DB: it, together with _ZOOM_LOCK below, serializes the
# read refresh_token -> oauth_zoom.refresh -> connections.set_secret critical section against both
# other threads in this process (API requests) and the separate `python -m pipeline.collect` CLI
# process, so Zoom's single-token-pair-per-app rotation never races two refreshes of the same token.
ZOOM_REFRESH_LOCK = db.DB_PATH.parent / "zoom_refresh.lock"
_ZOOM_LOCK = threading.Lock()
# 5 minutes of headroom before the cached access token's real expiry, so a caller never presents a
# token that expires mid-request.
_ZOOM_EXPIRY_MARGIN = 300


@dataclass
class _ZoomCache:
    access_token: str
    expires_at: float  # time.monotonic() seconds
    scope: set
    # sha256 of the refresh token this access token was derived from, never the token itself — a
    # log line or a debugger that reprs the cache cannot leak it. The store is the only source of
    # truth for the live refresh token; this is only how the cache notices it changed underneath
    # (a new browser sign-in rotated it outside this process) and drops itself.
    refresh_hash: str

    def as_dict(self) -> dict:
        return {"access_token": self.access_token, "scope": self.scope}


_zoom_cache: Optional[_ZoomCache] = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _zoom_cache_valid(cache: Optional[_ZoomCache], refresh_hash: str) -> TypeGuard[_ZoomCache]:
    # TypeGuard (not plain bool): every caller reads cache.as_dict() right after this check,
    # and pyright can only narrow Optional[_ZoomCache] -> _ZoomCache through the guard form.
    return (
        cache is not None
        and cache.refresh_hash == refresh_hash
        and (cache.expires_at - time.monotonic()) > _ZOOM_EXPIRY_MARGIN
    )


def _zoom_oauth_token_data(view: "connections.ConnectionView") -> dict:
    """
    {"access_token": str, "scope": set[str]} for an OAuth-mode Zoom connection, refreshing and
    rotating the stored refresh token when the cached one is missing or near expiry.

    The new refresh token is persisted with connections.set_secret BEFORE it is cached or returned:
    a crash after persisting simply means the next call refreshes again with the newest token; a
    crash before persisting leaves the store holding the token that was just used, which Zoom will
    reject on its next use, which is safe (ZoomAuthError, not silent data loss).

    Raises ZoomAuthError when there is no client id/secret/refresh token stored, or Zoom rejected
    the stored refresh token. Raises oauth_zoom.RefreshFailed on any other refresh failure
    (network, unreadable response) — a transient error, distinct from ZoomAuthError, so callers do
    not tell a user to sign in again for an outage.
    """
    from pipeline import oauth_zoom  # lazy: see the module-level import comment above

    global _zoom_cache
    connection_id = view.id
    client_id = view.config.get("client_id")
    client_secret = connections.get_secret(connection_id, "client_secret")
    if not client_id or not client_secret:
        raise ZoomAuthError(_ZOOM_NOT_SIGNED_IN)

    with _ZOOM_LOCK:
        refresh_token = connections.get_secret(connection_id, "refresh_token")
        if not refresh_token:
            raise ZoomAuthError(_ZOOM_NOT_SIGNED_IN)
        refresh_hash = _hash_token(refresh_token)
        if _zoom_cache_valid(_zoom_cache, refresh_hash):
            return _zoom_cache.as_dict()

        ZOOM_REFRESH_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(ZOOM_REFRESH_LOCK, "a+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                # Re-read: another process may have rotated the refresh token, or refreshed and
                # updated this process's cache, while this call waited for the file lock.
                current_refresh = connections.get_secret(connection_id, "refresh_token")
                if not current_refresh:
                    raise ZoomAuthError(_ZOOM_NOT_SIGNED_IN)
                current_hash = _hash_token(current_refresh)
                if _zoom_cache_valid(_zoom_cache, current_hash):
                    return _zoom_cache.as_dict()

                try:
                    tokens = oauth_zoom.refresh(client_id, client_secret, current_refresh)
                except oauth_zoom.RefreshTokenRejected:
                    raise ZoomAuthError(_ZOOM_SIGN_IN_EXPIRED) from None

                new_refresh = tokens["refresh_token"]
                connections.set_secret(connection_id, "refresh_token", new_refresh)

                _zoom_cache = _ZoomCache(
                    access_token=tokens["access_token"],
                    expires_at=time.monotonic() + tokens["expires_in"],
                    scope=set((tokens.get("scope") or "").split()),
                    refresh_hash=_hash_token(new_refresh),
                )
                return _zoom_cache.as_dict()
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def zoom_mode() -> str:
    """"oauth" or "s2s" for the configured Zoom source; "s2s" when there is no stored connection
    (a pure .env setup with no vault), same fallback zoom_token() and check_zoom() use."""
    view = connections.get("zoom")
    return connections.zoom_auth_mode(view) if view is not None else "s2s"


def zoom_token() -> str:
    """
    Access token for collectors, s2s or oauth depending on how Zoom is configured.

    Raises RuntimeError/requests.RequestException in s2s mode (unchanged), or ZoomAuthError /
    oauth_zoom.RefreshFailed in oauth mode.
    """
    view = connections.get("zoom")
    if view is None or connections.zoom_auth_mode(view) != "oauth":
        return zoom_token_response()["access_token"]
    return _zoom_oauth_token_data(view)["access_token"]


def check_zoom() -> HealthResult:
    """
    The Test button's Zoom check. Fetches (or refreshes) the same token zoom_token() would hand a
    collector — never a second, independent refresh — and reports the scopes it granted.
    """
    view = connections.get("zoom")
    if view is not None and connections.zoom_auth_mode(view) == "oauth":
        return _check_zoom_oauth(view)
    return _check_zoom_s2s(view)


def _check_zoom_s2s(view: Optional["connections.ConnectionView"]) -> HealthResult:
    try:
        data = zoom_token_response()
    except requests.RequestException as exc:
        return HealthResult("zoom", "error", str(exc))
    except RuntimeError as exc:
        return HealthResult("zoom", "error", str(exc))

    granted = set((data.get("scope") or "").split())
    required = set(ZOOM_SCOPES)
    # No stored view means a pure .env setup: there is no include_transcripts flag to read, so the
    # base scope set is all that is required, to avoid breaking a configuration that predates it.
    if view is not None and connections.zoom_include_transcripts(view):
        required.add(ZOOM_TRANSCRIPT_ADMIN_SCOPE)
    missing = _missing(required, granted)
    if missing:
        # Usually the app creator's role couldn't grant the admin scopes — recreate as account owner.
        return HealthResult("zoom", "error", f"token valid but missing scopes: {missing}")

    return HealthResult("zoom", "ok", f"token valid, expires_in={data['expires_in']}s, {len(granted)} scopes")


def _check_zoom_oauth(view: "connections.ConnectionView") -> HealthResult:
    from pipeline import oauth_zoom  # lazy: see the module-level import comment above

    try:
        data = _zoom_oauth_token_data(view)
    except ZoomAuthError as exc:
        return HealthResult("zoom", "error", str(exc))
    except oauth_zoom.RefreshFailed as exc:
        return HealthResult("zoom", "error", str(exc))

    granted = data["scope"]
    required = set(ZOOM_USER_SCOPES)
    if connections.zoom_include_transcripts(view):
        required.add(ZOOM_TRANSCRIPT_SCOPE)
    missing = _missing(required, granted)
    if missing:
        return HealthResult("zoom", "error", f"token valid but missing scopes: {missing}")

    return HealthResult("zoom", "ok", f"signed in, token valid, {len(granted)} scopes")


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
    # A per-label pair wins so one account can use its own OAuth client; today's configs only
    # declare the shared pair and keep working through the fallback.
    upper = label.upper()
    client_id = env.get(f"GMAIL_{upper}_CLIENT_ID") or env.get("GMAIL_CLIENT_ID")
    client_secret = env.get(f"GMAIL_{upper}_CLIENT_SECRET") or env.get("GMAIL_CLIENT_SECRET")
    if not all([client_id, client_secret]):
        raise RuntimeError("Missing GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env")
    refresh_token = gmail_accounts().get(label.lower())
    if not refresh_token:
        raise RuntimeError(
            f"Not signed in yet: use Sign in, or run auth/gmail_bootstrap.py {label} (no GMAIL_{label.upper()}_REFRESH_TOKEN)"
        )

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
    # A connection created in the UI has no token until its first sign-in. Answer before any
    # network call: exchanging an empty refresh token would only produce a confusing Google error.
    if not gmail_accounts().get(label.lower()):
        return HealthResult(source, "error", "Not signed in yet: use Sign in")
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


def check_domains() -> HealthResult:
    """
    "domains" is always healthy: it reads only the local DomainSnapshot table (no network, no
    credentials — see pipeline.collectors.domains), so there is nothing to authenticate and an
    empty portfolio is not a failure. Only a broken DB session counts as unhealthy.

    Imported locally, matching dispatch()'s own local import of pipeline.collectors.domains: this
    module is imported by nearly everything (see module docstring), so a module-level import of the
    database layer here should stay avoidable even though pipeline.db itself doesn't import back.
    """
    from sqlmodel import select

    from pipeline import db
    from pipeline.db import Domain

    try:
        with db.get_session() as session:
            count = len(session.exec(select(Domain)).all())
    except Exception as exc:  # noqa: BLE001 — a health check must report, never crash the run
        return HealthResult("domains", "error", type(exc).__name__)
    return HealthResult("domains", "ok", f"{count} domain(s) tracked")


def check_jobs() -> HealthResult:
    """
    "jobs" is always healthy: it reads only the local JobCompany table (no network, no
    credentials — see pipeline.collectors.jobs), so there is nothing to authenticate and an empty
    watchlist is not a failure. Only a broken DB session counts as unhealthy.

    Imported locally, matching dispatch()'s own local import of pipeline.collectors.jobs and
    check_domains()'s local import of pipeline.db: this module is imported by nearly everything
    (see module docstring), so a module-level import of the database layer here should stay
    avoidable even though pipeline.db itself doesn't import back.
    """
    from sqlmodel import select

    from pipeline import db
    from pipeline.db import JobCompany

    try:
        with db.get_session() as session:
            count = len(session.exec(select(JobCompany)).all())
    except Exception as exc:  # noqa: BLE001 — a health check must report, never crash the run
        return HealthResult("jobs", "error", type(exc).__name__)
    return HealthResult("jobs", "ok", f"{count} companies tracked")


def known_sources() -> list[str]:
    """Every source id the current .env declares: m365_<alias>, zoom, slack_<label>, gmail_<label>,
    plus the always-available "domains" and "jobs" sources (local DB, no .env entry needed)."""
    return (
        [f"m365_{a}" for a in m365_aliases()]
        + ["zoom"]
        + [f"slack_{l}" for l in slack_workspaces()]
        + [f"gmail_{l}" for l in gmail_accounts()]
        + ["domains"]
        + ["jobs"]
    )


def check_all_configured(active_sources: dict[str, bool]) -> list[HealthResult]:
    """Runs whichever declared sources are toggled on in config; skips the rest silently."""
    results: list[HealthResult] = []

    for alias in m365_aliases():
        if active_sources.get(f"m365_{alias}", False):
            results.append(check_m365(alias))

    if active_sources.get("zoom", False):
        results.append(check_zoom())

    if active_sources.get("domains", False):
        results.append(check_domains())

    if active_sources.get("jobs", False):
        results.append(check_jobs())

    for label, token in slack_workspaces().items():
        if active_sources.get(f"slack_{label}", False):
            results.append(check_slack(label, token))

    for label in gmail_accounts():
        if active_sources.get(f"gmail_{label}", False):
            results.append(check_gmail(label))

    return results
