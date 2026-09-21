"""
Browser sign-in for a Gmail connection: start the Google consent, then finish it from either the pasted
loopback URL or the provider's callback (docs/_research/2026-09-21_management-ui.md sections 4 and 6).

The pure OAuth helpers live in pipeline.oauth_google and the pending-flow store in pipeline.oauth_flows;
this module only sequences them around the connection store.

Design decisions:

* paste_back uses a fixed loopback redirect (PASTE_BACK_PORT). Nothing listens there and nothing has to:
  a Desktop client accepts any loopback port, the browser's "cannot connect" page keeps the code in the
  address bar, and the user pastes that URL back. callback needs an https PUBLIC_BASE_URL, so it is
  refused without one instead of guessing from a request's Host header.
* The code_verifier and the redirect_uri are kept server-side in the pending flow. The exchange always
  presents the STORED redirect_uri: a pasted URL is untrusted text that only supplies `code` and `state`.
* finish_paste parses the URL before it touches the store, so a malformed paste does not burn the flow and
  the user can paste again. Once the nonce holder has consumed the flow, any later failure (state
  mismatch, denied consent, a rejected exchange) ends it: an authorization code is single use anyway, so
  the sign-in is started again.
* Every failure is a FlowError subclass with a fixed message and no arguments, so one `except FlowError`
  in the route layer covers state, expiry, consent and exchange problems, and a code, verifier, token or
  client secret cannot reach a message by construction. Google's own error text is dropped on purpose.
* Only the refresh token is persisted, through connections.set_secret (encrypted). Nothing is returned
  but the secret-free ConnectionView.
"""
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn, Optional

from pipeline import connections, oauth_google
from pipeline.health import GMAIL_SCOPE
from pipeline.oauth_flows import (
    MODES,
    FlowError,
    FlowStore,
    InvalidCallbackConfig,
    PendingFlow,
    build_callback_url,
    parse_pasted_url,
)

PROVIDER = "google"
# Desktop OAuth clients accept any loopback port and nothing needs to listen on it.
PASTE_BACK_PORT = 8765
PASTE_BACK_REDIRECT_URI = f"http://127.0.0.1:{PASTE_BACK_PORT}"

_PASTE_BACK_INSTRUCTIONS = (
    "Open the link and approve read-only Gmail access. Your browser will then show a page that cannot be "
    "reached: that is expected. Copy the full address from the address bar and paste it here straight away."
)
_CALLBACK_INSTRUCTIONS = "Open the link and approve read-only Gmail access. You will be returned here automatically."


class InvalidMode(FlowError):
    """The mode is neither paste_back nor callback."""

    message = "sign-in mode must be paste_back or callback"


class UnknownConnection(FlowError):
    """No Gmail connection has this id (or it is not a Gmail connection)."""

    message = "no such Gmail connection"


class ClientCredentialsMissing(FlowError):
    """The connection lacks the OAuth client id or client secret the consent and exchange need."""

    message = "the Gmail connection needs both a client id and a client secret before signing in"


class CallbackUnavailable(InvalidCallbackConfig):
    """Callback mode was requested but no PUBLIC_BASE_URL is configured."""

    message = "callback sign-in needs PUBLIC_BASE_URL set to an https address; use paste_back instead"


class StateMismatch(FlowError):
    """The pasted URL answers a different sign-in than the one this browser started."""

    message = "the pasted URL does not belong to this sign-in; start again and paste the newest address"


class ConsentDenied(FlowError):
    """The user declined (or cancelled) on Google's consent screen."""

    message = "Google sign-in was cancelled or denied"


class ProviderError(FlowError):
    """Google answered with an error other than a denial, or with no code."""

    message = "Google reported an error during sign-in; start again"


class ExchangeFailed(FlowError):
    """The token endpoint rejected the code or could not be reached."""

    message = "Google did not accept the sign-in code (expired, already used, or unreachable); start again"


class ScopeNotGranted(FlowError):
    """The consent did not include read-only Gmail access."""

    message = "read-only Gmail access was not granted; tick the Gmail box on the consent screen"


class NoRefreshToken(FlowError):
    """Google issued no refresh token, which it does when the app is already authorized."""

    message = "Google returned no refresh token; remove this app under your Google account permissions and start again"


@dataclass(frozen=True)
class StartResult:
    """
    What the caller needs to begin a sign-in. The nonce is the only copy: it becomes the browser's
    HttpOnly cookie. It and the auth URL (which carries the state) stay out of repr.
    """

    flow_id: str
    auth_url: str = field(repr=False)
    mode: str
    expires_at: datetime
    nonce: str = field(repr=False)
    instructions: str


def _client_credentials(connection_id: str) -> tuple[str, str]:
    view = connections.get(connection_id)
    if view is None or view.kind != "gmail":
        raise UnknownConnection()
    client_id = view.config.get("client_id")
    client_secret = connections.get_secret(connection_id, "client_secret")
    if not isinstance(client_id, str) or not client_id or not client_secret:
        raise ClientCredentialsMissing()
    return client_id, client_secret


def start(
    connection_id: str,
    mode: str,
    *,
    store: FlowStore,
    public_base_url: Optional[str] = None,
) -> StartResult:
    """
    Register a pending sign-in and build the Google consent URL for it.

    :param mode: "paste_back" (loopback redirect) or "callback" (https redirect on public_base_url).
    :param public_base_url: the configured PUBLIC_BASE_URL; required for callback.
    :raises InvalidMode: unknown mode.
    :raises UnknownConnection: no Gmail connection with this id.
    :raises ClientCredentialsMissing: the connection has no client id or no client secret.
    :raises CallbackUnavailable: callback without public_base_url.
    :raises InvalidCallbackConfig: public_base_url is not an https URL.
    :raises SecretKeyMissing: no vault installed; SecretDecryptError: stored secrets cannot be opened.
    """
    if mode not in MODES:
        raise InvalidMode()
    client_id, _ = _client_credentials(connection_id)
    if mode == "callback":
        if public_base_url is None:
            raise CallbackUnavailable()
        redirect_uri = build_callback_url(public_base_url, PROVIDER)
    else:
        redirect_uri = PASTE_BACK_REDIRECT_URI

    verifier, challenge = oauth_google.make_pkce()
    state = secrets.token_urlsafe(32)
    flow, nonce = store.create(
        PROVIDER,
        connection_id,
        mode,
        state,
        redirect_uri,
        {"code_verifier": verifier},
    )
    return StartResult(
        flow_id=flow.flow_id,
        auth_url=oauth_google.build_auth_url(client_id, redirect_uri, challenge, state),
        mode=mode,
        expires_at=flow.expires_at,
        nonce=nonce,
        instructions=_PASTE_BACK_INSTRUCTIONS if mode == "paste_back" else _CALLBACK_INSTRUCTIONS,
    )


def _raise_provider_error(error: str) -> NoReturn:
    raise ConsentDenied() if error == "access_denied" else ProviderError()


def finish(flow: PendingFlow, code: str) -> connections.ConnectionView:
    """
    Exchange the code for tokens and store the refresh token on the flow's connection.

    :param flow: a flow already consumed from the store (nonce and state verified by the caller).
    :param code: the authorization code; presented once and never retained.
    :returns: the connection's view (secret names only) after the refresh token is stored.
    :raises UnknownConnection: the connection was deleted mid-flow.
    :raises ClientCredentialsMissing: the client id or secret was removed mid-flow.
    :raises ExchangeFailed: no code, or the token endpoint rejected it or was unreachable.
    :raises ScopeNotGranted: Gmail read-only was not granted.
    :raises NoRefreshToken: the response carried no refresh token.
    :raises ConnectionError: (pipeline.connections) if the refresh token cannot be stored.
    """
    if not isinstance(code, str) or not code:
        raise ProviderError()
    client_id, client_secret = _client_credentials(flow.connection_id)
    try:
        tokens = oauth_google.exchange_code(
            client_id, client_secret, code, flow.payload["code_verifier"], flow.redirect_uri
        )
    except oauth_google.TokenExchangeError:
        # from None: the original message can carry Google's error text and the request's URL.
        raise ExchangeFailed() from None
    try:
        refresh_token = oauth_google.assert_granted(tokens)
    except oauth_google.GrantError:
        if GMAIL_SCOPE not in set((tokens.get("scope") or "").split()):
            raise ScopeNotGranted() from None
        raise NoRefreshToken() from None
    return connections.set_secret(flow.connection_id, "refresh_token", refresh_token)


def finish_paste(store: FlowStore, flow_id: str, pasted_url: str, nonce: Optional[str]) -> connections.ConnectionView:
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
    return finish(flow, parsed["code"] or "")


def finish_callback(
    store: FlowStore,
    state: Optional[str],
    code: Optional[str],
    nonce: Optional[str],
    error: Optional[str] = None,
) -> connections.ConnectionView:
    """
    Finish a callback sign-in from the query parameters Google sent to /api/oauth/callback/google.

    :param error: Google's `error` parameter, when the user denied consent.
    :raises UnknownFlow, ExpiredFlow, NonceMismatch, ProviderMismatch: from the store; the flow is looked
        up by state, and only the browser holding the nonce can consume it.
    :raises ConsentDenied, ProviderError: `error` was present.
    :raises: everything finish() raises.
    """
    flow = store.consume(state=state, nonce=nonce, provider=PROVIDER)
    if error:
        _raise_provider_error(error)
    return finish(flow, code or "")
