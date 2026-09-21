"""
Browser sign-in routes for Gmail (provider google), Microsoft 365 (provider microsoft) and Zoom
(provider zoom) (docs/_research/2026-09-21_management-ui.md section 6, RFC 9700).

Design decisions:

- The API has no login, so a sign-in is bound to the browser that started it by an HttpOnly,
  SameSite=Lax nonce cookie named ss_oauth_<flow_id>, set at start. Lax is what lets it ride along
  on the provider's top-level GET redirect back to the callback. The nonce is REQUIRED on every
  FlowStore.consume, paste_back included, so the paste route reads the cookie too. A wrong or
  missing nonce fails without consuming the flow (that is the store's rule), so somebody who saw a
  state in an address bar cannot cancel the owner's sign-in.
- The callback is unauthenticated by necessity (the provider's redirect cannot carry the custom
  header) and it is a GET, which the request guard lets through. Everything it can do is gated by
  the state plus the nonce cookie. It only ever answers 302 to a FIXED target: the first usable
  web origin from configuration plus /connections?oauth=ok, or ?oauth=error&reason=<code> with the
  code taken from _REASONS. Nothing from the query string, the provider's error text or an
  exception reaches Location, a cookie, a log line or a response body (RFC 9700: no redirect to
  a URL taken from a parameter). An unknown provider path gets the same generic error redirect.
- The callback does not know the flow id (the provider echoes only the state), so it cannot name
  the cookie. It tries each ss_oauth_ cookie the browser sent: a wrong nonce is a no-op in the
  store, so trying the candidates in turn is safe, and the one that matches is the flow's own.
  Only that cookie is deleted. A blanket delete of every ss_oauth_ cookie would let any link that
  sends the victim to a junk callback URL strand a sign-in they had under way.
- A pending flow is consumed by the provider module, which returns different shapes (a bare
  ConnectionView for Gmail, FinishResult for Microsoft). The router normalizes them, and one
  FlowError mapping covers both because Microsoft's errors subclass Gmail's. Every FlowError has a
  fixed class-constant message, so it is safe to return as is; anything else is reported by type
  name only.
- The store is a module singleton (FLOWS). Handlers read it at call time so a test can swap it.
- Sign-in calls block on the network, so handlers are plain `def` and run in the threadpool.
"""
import logging
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from api.routers.connections import ConnectionOut, _out_for
from api.security import normalize_origin
from api.serialize import iso_z
from pipeline import connections, health, oauth_gmail, oauth_m365, oauth_zoom
from pipeline.clock import utcnow
from pipeline.crypto import SecretDecryptError, SecretKeyMissing
from pipeline.oauth_flows import (
    MAX_PENDING,
    ExpiredFlow,
    FlowError,
    FlowStore,
    InvalidCallbackConfig,
    InvalidPastedUrl,
    NonceMismatch,
    ProviderMismatch,
    UnknownFlow,
)

router = APIRouter(tags=["oauth"])

_log = logging.getLogger(__name__)

FLOWS = FlowStore()

COOKIE_PREFIX = "ss_oauth_"
COOKIE_PATH = "/api/oauth"

# The connection kind each provider path signs in.
_KIND_FOR_PROVIDER = {"google": "gmail", "microsoft": "m365", "zoom": "zoom"}

# Bound on what one callback will read from the query string; the provider modules only compare
# `error` to a constant and hand `code` to the token endpoint.
_MAX_PARAM = 2048

# Every value ?reason= can ever carry. The redirect is built from this set, never from input.
_REASONS = frozenset(
    {
        "invalid_request",
        "expired",
        "denied",
        "provider_error",
        "exchange_failed",
        "scope_missing",
        "no_refresh_token",
        "failed",
    }
)


class StartBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    connection_id: str = Field(min_length=1, max_length=200)
    mode: str = Field(pattern="^(paste_back|callback)$")


class StartOut(BaseModel):
    flow_id: str
    auth_url: str
    mode: str
    expires_at: str
    instructions: str


class PasteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    flow_id: str = Field(min_length=1, max_length=128)
    # Bounded here as well as by the parser: pydantic's own message never echoes the value.
    url: str = Field(min_length=1, max_length=8192)


class PasteOut(BaseModel):
    status: str
    connection: ConnectionOut
    account: Optional[str] = None


def _coded(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


# Order matters: a subclass is listed before its base. Microsoft's errors subclass the Gmail ones,
# so matching the Gmail class covers both providers.
_FLOW_ERRORS: tuple[tuple[type[FlowError], int, str], ...] = (
    (InvalidPastedUrl, 400, "invalid_pasted_url"),
    (UnknownFlow, 404, "unknown_flow"),
    (ExpiredFlow, 410, "flow_expired"),
    (NonceMismatch, 403, "nonce_mismatch"),
    (ProviderMismatch, 400, "provider_mismatch"),
    (oauth_gmail.StateMismatch, 400, "state_mismatch"),
    (oauth_gmail.ConsentDenied, 400, "consent_denied"),
    (oauth_gmail.ScopeNotGranted, 400, "scope_not_granted"),
    (oauth_gmail.NoRefreshToken, 400, "no_refresh_token"),
    (oauth_gmail.ProviderError, 502, "provider_error"),
    (oauth_gmail.ExchangeFailed, 502, "exchange_failed"),
    (oauth_m365.CacheWriteFailed, 503, "cache_write_failed"),
    (InvalidCallbackConfig, 400, "callback_unavailable"),
    (oauth_gmail.InvalidMode, 400, "invalid_mode"),
    (oauth_gmail.UnknownConnection, 404, "connection_not_found"),
    (oauth_gmail.ClientCredentialsMissing, 400, "client_credentials_missing"),
    (oauth_m365.InvalidAlias, 400, "invalid_alias"),
)


def _translate(exc: Exception) -> HTTPException:
    """Maps what the provider modules raise to {detail: {code, message}}; never str() of an unexpected error."""
    if isinstance(exc, FlowError):
        for kind, status_code, code in _FLOW_ERRORS:
            if isinstance(exc, kind):
                return _coded(status_code, code, exc.message)
        return _coded(400, "sign_in_failed", exc.message)
    if isinstance(exc, connections.ConnectionNotFound):
        return _coded(404, "connection_not_found", "Connection not found")
    if isinstance(exc, connections.ConnectionError):
        return _coded(409, "connection_error", "The connection could not be updated")
    if isinstance(exc, SecretKeyMissing):
        return _coded(503, "secret_key_missing", "No encryption key is configured; secrets cannot be stored")
    if isinstance(exc, SecretDecryptError):
        return _coded(503, "secret_decrypt_failed", "Stored secrets cannot be decrypted with the installed key")
    raise exc


_HANDLED = (FlowError, connections.ConnectionError, SecretKeyMissing, SecretDecryptError)


def _start(provider: str, connection_id: str, mode: str) -> oauth_gmail.StartResult:
    # provider is always a key of _KIND_FOR_PROVIDER by the time this is called (both callers check
    # first); the final raise keeps an unknown provider from ever falling through to a real flow.
    base = health.public_base_url()
    if provider == "google":
        return oauth_gmail.start(connection_id, mode, store=FLOWS, public_base_url=base)
    if provider == "microsoft":
        return oauth_m365.start(connection_id, mode, store=FLOWS, public_base_url=base)
    if provider == "zoom":
        return oauth_zoom.start(connection_id, mode, store=FLOWS, public_base_url=base)
    raise AssertionError(f"unhandled provider {provider!r}")


def _finish_paste(
    provider: str, flow_id: str, url: str, nonce: Optional[str]
) -> Union[connections.ConnectionView, oauth_m365.FinishResult]:
    if provider == "google":
        return oauth_gmail.finish_paste(FLOWS, flow_id, url, nonce)
    if provider == "microsoft":
        return oauth_m365.finish_paste(FLOWS, flow_id, url, nonce)
    if provider == "zoom":
        # Zoom is a confidential client offering callback mode only; there is no oauth_zoom.finish_paste.
        # Raised without touching the flow or its nonce cookie, same as a paste_back start would raise.
        raise oauth_zoom.PasteBackNotSupported()
    raise AssertionError(f"unhandled provider {provider!r}")


def _finish_callback(
    provider: str, state: Optional[str], code: Optional[str], nonce: Optional[str], error: Optional[str]
) -> object:
    if provider == "google":
        return oauth_gmail.finish_callback(FLOWS, state, code, nonce, error)
    if provider == "microsoft":
        return oauth_m365.finish_callback(FLOWS, state, code, nonce, error)
    if provider == "zoom":
        return oauth_zoom.finish_callback(FLOWS, state, code, nonce, error)
    raise AssertionError(f"unhandled provider {provider!r}")


def _secure() -> bool:
    # public_base_url() is None unless it is https, so "set" means the UI is served over TLS.
    return health.public_base_url() is not None


def _delete_nonce_cookie(response: Response, name: str) -> None:
    # Path, HttpOnly, SameSite and Secure repeat the set_cookie attributes so the browser matches the cookie.
    response.delete_cookie(name, path=COOKIE_PATH, secure=_secure(), httponly=True, samesite="lax")


@router.post("/oauth/{provider}/start", response_model=StartOut)
def start_sign_in(provider: str, body: StartBody, response: Response) -> StartOut:
    """
    Begins a sign-in and sets the browser-binding nonce cookie.

    The auth_url is the only place the OAuth state leaves the server; the PKCE verifier and the
    nonce never do (the nonce goes only into the HttpOnly cookie).

    Raises 404 (unknown provider or connection), 400 (connection kind does not match the provider,
    callback mode without an https PUBLIC_BASE_URL, missing client credentials, unusable alias),
    502 (provider could not start a flow), 503 (secret store).
    """
    expected_kind = _KIND_FOR_PROVIDER.get(provider)
    if expected_kind is None:
        raise _coded(404, "unknown_provider", "Unknown sign-in provider")
    view = connections.get(body.connection_id)
    if view is None:
        raise _coded(404, "connection_not_found", "Connection not found")
    if view.kind != expected_kind:
        raise _coded(400, "provider_kind_mismatch", "This connection cannot sign in with that provider")
    if body.mode == "callback" and health.public_base_url() is None:
        raise _coded(400, "callback_unavailable", oauth_gmail.CallbackUnavailable.message)
    try:
        result = _start(provider, body.connection_id, body.mode)
    except _HANDLED as exc:
        raise _translate(exc) from None

    max_age = max(1, int((result.expires_at - utcnow()).total_seconds()))
    response.set_cookie(
        COOKIE_PREFIX + result.flow_id,
        result.nonce,
        max_age=max_age,
        path=COOKIE_PATH,
        secure=_secure(),
        httponly=True,
        samesite="lax",
    )
    return StartOut(
        flow_id=result.flow_id,
        auth_url=result.auth_url,
        mode=result.mode,
        expires_at=iso_z(result.expires_at) or "",
        instructions=result.instructions,
    )


@router.post("/oauth/{provider}/paste", response_model=PasteOut)
def paste_sign_in(provider: str, body: PasteBody, request: Request, response: Response) -> PasteOut:
    """
    Finishes a paste_back sign-in from the address the user copied. The pasted text is parsed, never
    fetched, and only supplies code and state.

    Raises 400 (unusable paste, state or provider mismatch, denied consent, scope or refresh token
    missing), 403 (no or wrong nonce cookie; the flow is NOT consumed), 404, 410 (expired), 502
    (token endpoint), 503 (secret store, token cache).
    """
    if provider not in _KIND_FOR_PROVIDER:
        raise _coded(404, "unknown_provider", "Unknown sign-in provider")
    cookie = COOKIE_PREFIX + body.flow_id
    try:
        result = _finish_paste(provider, body.flow_id, body.url, request.cookies.get(cookie))
    except _HANDLED as exc:
        raise _translate(exc) from None

    if isinstance(result, oauth_m365.FinishResult):
        view, account = result.connection, result.account
    else:
        view, account = result, None
    _delete_nonce_cookie(response, cookie)
    return PasteOut(status="connected", connection=_out_for(view), account=account)


def _ui_origin() -> Optional[str]:
    for raw in health.web_origins():
        origin = normalize_origin(raw)
        if origin is not None:
            return origin
    return None


def _reason_for(exc: Exception) -> str:
    if isinstance(exc, oauth_gmail.ConsentDenied):
        return "denied"
    if isinstance(exc, ExpiredFlow):
        return "expired"
    if isinstance(exc, (UnknownFlow, NonceMismatch, ProviderMismatch, oauth_gmail.StateMismatch)):
        return "invalid_request"
    if isinstance(exc, oauth_gmail.ScopeNotGranted):
        return "scope_missing"
    if isinstance(exc, oauth_gmail.NoRefreshToken):
        return "no_refresh_token"
    if isinstance(exc, oauth_gmail.ExchangeFailed):
        return "exchange_failed"
    if isinstance(exc, oauth_gmail.ProviderError):
        return "provider_error"
    return "failed"


def _finish_redirect(reason: Optional[str], delete_cookie: Optional[str]) -> Response:
    """The only response the callback produces. `reason` must come from _REASONS, or be None for success."""
    origin = _ui_origin()
    outcome = "oauth=ok" if reason is None else f"oauth=error&reason={reason if reason in _REASONS else 'failed'}"
    if origin is None:
        # No usable UI origin is configured, so there is nowhere safe to send the browser.
        response: Response = PlainTextResponse(
            "Sign-in finished. Return to SignalSlate." if reason is None else "Sign-in failed. Return to SignalSlate.",
            status_code=200 if reason is None else 400,
        )
    else:
        response = RedirectResponse(f"{origin}/connections?{outcome}", status_code=302)
    # The address the browser just visited carried the code and state.
    response.headers["Cache-Control"] = "no-store"
    if delete_cookie is not None:
        _delete_nonce_cookie(response, delete_cookie)
    return response


def _param(request: Request, name: str) -> Optional[str]:
    value = request.query_params.get(name)
    return value[:_MAX_PARAM] if value else None


@router.get("/oauth/callback/{provider}", status_code=302, response_class=RedirectResponse)
def oauth_callback(provider: str, request: Request) -> Response:
    """
    The provider's redirect target: the one unauthenticated route, so it is a fixed-output funnel.

    Always answers 302 to the UI's /connections page with oauth=ok, or oauth=error and a reason
    from a fixed set. The state and code are read from the query and never logged or echoed.
    """
    if provider not in _KIND_FOR_PROVIDER:
        return _finish_redirect("invalid_request", None)
    state, code, error = _param(request, "state"), _param(request, "code"), _param(request, "error")
    candidates = [(name, value) for name, value in request.cookies.items() if name.startswith(COOKIE_PREFIX)]

    # The nonce is checked before anything else in consume(), so a candidate that is not this flow's
    # raises NonceMismatch with no effect and the next one is tried.
    for name, nonce in candidates[:MAX_PENDING] or [("", None)]:
        try:
            _finish_callback(provider, state, code, nonce, error)
        except NonceMismatch:
            continue
        except ProviderMismatch:
            # The nonce was right but the state belongs to the other provider: the flow is still
            # pending, so its cookie stays.
            return _finish_redirect("invalid_request", None)
        except (UnknownFlow, ExpiredFlow) as exc:
            # The lookup is by state, so every candidate would answer the same.
            return _finish_redirect(_reason_for(exc), None)
        except Exception as exc:  # noqa: BLE001 — anything else ends the flow; only the type is logged
            _log.warning("oauth callback for %s failed: %s", provider, type(exc).__name__)
            return _finish_redirect(_reason_for(exc), name)
        return _finish_redirect(None, name)
    return _finish_redirect("invalid_request", None)
