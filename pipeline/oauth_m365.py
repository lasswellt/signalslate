"""
Browser sign-in for a Microsoft 365 connection: start the Entra consent, then finish it from either the
pasted loopback URL or the provider's callback (docs/_research/2026-09-21_management-ui.md section 5).

Same public shape as pipeline.oauth_gmail (start, finish_paste, finish_callback) so the API treats the
providers alike; the provider-neutral error classes are imported from there and the Microsoft ones
subclass them, so one `except` in the route layer covers both while each message names the right provider.

Design decisions:

* MSAL owns PKCE, state and the auth URL. initiate_auth_code_flow returns a dict that includes the
  code_verifier; the WHOLE dict is kept server-side in the pending flow and handed back to
  acquire_token_by_auth_code_flow, which is why nothing of it is ever returned to the browser.
* paste_back redirects to http://localhost: Entra ignores the port on a localhost redirect, and that URI
  is what the app registration holds today. The default response_mode=query is required: form_post would
  put the code in a POST body, and the address bar the user copies from would show nothing.
* Microsoft authorization codes live about a minute, so the paste is exchanged immediately and the
  instructions say so. finish_paste parses the URL before it touches the store, so a malformed paste
  does not burn the flow; once the nonce holder has consumed it, any later failure ends it.
* The exchange writes the same tokens/<alias>_cache.bin the CLI bootstrap and the collectors use, under
  tokencache.locked (MSAL rotates refresh tokens, so an unserialized read-modify-write strands one) and
  saved atomically with mode 0600. health.TOKEN_DIR is passed explicitly because that is the directory
  the health check and collector read from.
* Every failure is a FlowError with a fixed message and no arguments, and exchange failures are raised
  `from None`: MSAL's error text and exception chain can carry the code, the verifier or the request
  URL. Microsoft's own error_description is dropped from the message on purpose; only its AADSTS number
  (digits only) is logged so an operator can look it up.
"""
import logging
import re
from dataclasses import dataclass
from typing import Any, NoReturn, Optional

import msal
import requests

from pipeline import connections, health, oauth_gmail, oauth_google, tokencache
from pipeline.oauth_flows import (
    MODES,
    FlowError,
    FlowStore,
    PendingFlow,
    build_callback_url,
    parse_pasted_url,
)

PROVIDER = "microsoft"
# Entra ignores the port of a localhost redirect, and nothing has to listen on it.
PASTE_BACK_REDIRECT_URI = "http://localhost"

_PASTE_BACK_INSTRUCTIONS = (
    "Open the link and sign in with the work account. Your browser will then show a page that cannot be "
    "reached: that is expected. Copy the full address from the address bar and paste it here straight "
    "away, because Microsoft sign-in codes expire after about a minute."
)
_CALLBACK_INSTRUCTIONS = (
    "Open the link and sign in with the work account. You will be returned here automatically."
)
_MAX_ACCOUNT_LENGTH = 320
_AADSTS_CODE = re.compile(r"AADSTS\d{1,7}")

_log = logging.getLogger(__name__)

InvalidMode = oauth_gmail.InvalidMode
CallbackUnavailable = oauth_gmail.CallbackUnavailable
StateMismatch = oauth_gmail.StateMismatch
StartResult = oauth_gmail.StartResult


class UnknownConnection(oauth_gmail.UnknownConnection):
    """No Microsoft 365 connection has this id (or it is not an m365 connection)."""

    message = "no such Microsoft 365 connection"


class InvalidAlias(FlowError):
    """The connection's alias cannot name a token cache file (tokencache's path-traversal guard)."""

    message = "the Microsoft 365 connection's alias cannot be used as a token cache name"


class ConsentDenied(oauth_gmail.ConsentDenied):
    """The user declined (or cancelled) on Microsoft's consent screen."""

    message = "Microsoft sign-in was cancelled or denied"


class ProviderError(oauth_gmail.ProviderError):
    """Microsoft answered with an error other than a denial, or with no code, or could not start a flow."""

    message = "Microsoft reported an error during sign-in; start again"


class ExchangeFailed(oauth_gmail.ExchangeFailed):
    """The token endpoint rejected the code, returned no access token, or could not be reached."""

    message = "Microsoft did not accept the sign-in code (expired, already used, or unreachable); start again"


class CacheWriteFailed(FlowError):
    """Microsoft accepted the code but the token cache file could not be written."""

    message = "the Microsoft sign-in succeeded but the token cache could not be saved; check the tokens directory"


@dataclass(frozen=True)
class FinishResult:
    """The secret-free connection view and the signed-in account's label (its preferred_username)."""

    connection: connections.ConnectionView
    account: Optional[str]


def _config(connection_id: str) -> dict[str, str]:
    view = connections.get(connection_id)
    if view is None or view.kind != "m365":
        raise UnknownConnection()
    alias, tenant_id, client_id = (view.config.get(name) for name in ("alias", "tenant_id", "client_id"))
    if not (isinstance(alias, str) and isinstance(tenant_id, str) and isinstance(client_id, str)):
        raise UnknownConnection()
    if not (tenant_id and client_id):
        raise UnknownConnection()
    try:
        tokencache.cache_path(alias)
    except ValueError:
        raise InvalidAlias() from None
    return {"alias": alias, "tenant_id": tenant_id, "client_id": client_id}


def start(
    connection_id: str,
    mode: str,
    *,
    store: FlowStore,
    public_base_url: Optional[str] = None,
) -> StartResult:
    """
    Register a pending sign-in and ask MSAL for the Entra consent URL.

    :param mode: "paste_back" (http://localhost redirect) or "callback" (https redirect on public_base_url).
    :param public_base_url: the configured PUBLIC_BASE_URL; required for callback.
    :raises InvalidMode: unknown mode.
    :raises UnknownConnection: no m365 connection with this id.
    :raises InvalidAlias: the alias is not a safe token cache name.
    :raises CallbackUnavailable: callback without public_base_url.
    :raises InvalidCallbackConfig: public_base_url is not an https URL.
    :raises ProviderError: MSAL could not build a flow (bad tenant, authority unreachable).
    """
    if mode not in MODES:
        raise InvalidMode()
    cfg = _config(connection_id)
    if mode == "callback":
        if public_base_url is None:
            raise CallbackUnavailable()
        redirect_uri = build_callback_url(public_base_url, PROVIDER)
    else:
        redirect_uri = PASTE_BACK_REDIRECT_URI

    try:
        app = health.m365_app(cfg, msal.SerializableTokenCache())
        flow = app.initiate_auth_code_flow(health.SCOPES, redirect_uri=redirect_uri)
    except (ValueError, requests.RequestException):
        raise ProviderError() from None
    if not isinstance(flow, dict) or not isinstance(flow.get("auth_uri"), str) or not flow.get("state"):
        raise ProviderError()

    pending, nonce = store.create(PROVIDER, connection_id, mode, str(flow["state"]), redirect_uri, flow)
    return StartResult(
        flow_id=pending.flow_id,
        auth_url=flow["auth_uri"],
        mode=mode,
        expires_at=pending.expires_at,
        nonce=nonce,
        instructions=_PASTE_BACK_INSTRUCTIONS if mode == "paste_back" else _CALLBACK_INSTRUCTIONS,
    )


def _raise_provider_error(error: str) -> NoReturn:
    raise ConsentDenied() if error == "access_denied" else ProviderError()


def _account_label(result: dict[str, Any], app: msal.PublicClientApplication) -> Optional[str]:
    claims = result.get("id_token_claims")
    label = claims.get("preferred_username") if isinstance(claims, dict) else None
    if not isinstance(label, str) or not label:
        accounts = app.get_accounts()
        label = accounts[0].get("username") if accounts else None
    return label[:_MAX_ACCOUNT_LENGTH] if isinstance(label, str) and label else None


def finish(flow: PendingFlow, auth_response: dict[str, str]) -> FinishResult:
    """
    Exchange the code through MSAL and save the per-alias token cache.

    :param flow: a flow already consumed from the store (nonce and state verified by the caller).
    :param auth_response: {code, state}; the redirect's other parameters are deliberately not forwarded.
    :returns: the connection's view and the account label.
    :raises UnknownConnection, InvalidAlias: the connection was deleted or changed mid-flow.
    :raises ProviderError: no code.
    :raises StateMismatch: MSAL rejected the state (usually a forged or stale response).
    :raises ExchangeFailed: Microsoft rejected the code, issued no access token, or was unreachable.
    :raises CacheWriteFailed: the token cache file could not be written.
    """
    if not auth_response.get("code"):
        raise ProviderError()
    cfg = _config(flow.connection_id)
    alias, token_dir = cfg["alias"], health.TOKEN_DIR

    with tokencache.locked(alias, token_dir=token_dir):
        cache = tokencache.load(alias, token_dir=token_dir) or msal.SerializableTokenCache()
        try:
            app = health.m365_app(cfg, cache)
        except (ValueError, requests.RequestException):
            raise ExchangeFailed() from None
        try:
            result = app.acquire_token_by_auth_code_flow(dict(flow.payload), auth_response)
        except requests.RequestException:
            raise ExchangeFailed() from None
        except ValueError:
            # MSAL raises ValueError for a state mismatch, "usually caused by CSRF".
            raise StateMismatch() from None
        if not isinstance(result, dict) or "error" in result or not result.get("access_token"):
            match = _AADSTS_CODE.search(str(result.get("error_description") or "")) if isinstance(result, dict) else None
            _log.warning("microsoft sign-in exchange failed for %s (%s)", flow.connection_id, match.group(0) if match else "no AADSTS code")
            raise ExchangeFailed()
        try:
            tokencache.save(alias, cache, token_dir=token_dir)
        except OSError:
            raise CacheWriteFailed() from None
        account = _account_label(result, app)

    view = connections.get(flow.connection_id)
    if view is None:
        raise UnknownConnection()
    return FinishResult(connection=view, account=account)


def finish_paste(store: FlowStore, flow_id: str, pasted_url: str, nonce: Optional[str]) -> FinishResult:
    """
    Finish a paste_back sign-in from the URL the user copied out of the address bar.

    :raises InvalidPastedUrl: (pipeline.oauth_flows) the paste is not an acceptable loopback redirect;
        the flow is NOT consumed, so the user can paste again.
    :raises UnknownFlow, ExpiredFlow, NonceMismatch: from the store (a wrong nonce leaves the flow in place).
    :raises StateMismatch: the pasted state is not this flow's.
    :raises ConsentDenied, ProviderError: the URL carries an error instead of a code.
    :raises: everything finish() raises.
    """
    parsed = parse_pasted_url(pasted_url)
    flow = store.consume(flow_id=flow_id, nonce=nonce, provider=PROVIDER)
    try:
        oauth_google.check_state(flow.state, parsed["state"])
    except oauth_google.StateMismatchError:
        raise StateMismatch() from None
    if parsed["error"]:
        _raise_provider_error(parsed["error"])
    return finish(flow, {"code": parsed["code"] or "", "state": parsed["state"] or ""})


def finish_callback(
    store: FlowStore,
    state: Optional[str],
    code: Optional[str],
    nonce: Optional[str],
    error: Optional[str] = None,
) -> FinishResult:
    """
    Finish a callback sign-in from the query parameters Microsoft sent to /api/oauth/callback/microsoft.

    :param error: Microsoft's `error` parameter, when the user denied consent.
    :raises UnknownFlow, ExpiredFlow, NonceMismatch, ProviderMismatch: from the store; the flow is looked
        up by state, and only the browser holding the nonce can consume it.
    :raises ConsentDenied, ProviderError: `error` was present.
    :raises: everything finish() raises.
    """
    flow = store.consume(state=state, nonce=nonce, provider=PROVIDER)
    if error:
        _raise_provider_error(error)
    return finish(flow, {"code": code or "", "state": flow.state})
