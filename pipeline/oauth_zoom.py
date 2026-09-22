"""
Browser sign-in for a Zoom connection: start the Zoom consent (callback mode only), finish it from the
provider's callback, and rotate the stored refresh token (docs/_research/2026-09-21_management-ui.md
Q2 and step 2).

Same public shape as pipeline.oauth_gmail (start, finish_callback) so the API treats every provider
alike; the provider-neutral error classes are imported from there and the Zoom ones subclass them, so
one `except FlowError` in the route layer covers all three providers. Unlike Gmail and Microsoft 365,
this module also holds its own pure endpoint helpers (Zoom's auth URL and token exchange): the task
folds oauth_google.py's role into this one module instead of a separate oauth_zoom_endpoints.py,
because Zoom is the only provider here with no SDK and no PKCE to keep pure logic separate from.

Design decisions:

* Confidential client, no PKCE. The code exchange POSTs to https://zoom.us/oauth/token with
  grant_type=authorization_code, code, redirect_uri (the STORED redirect_uri from the pending flow) and
  HTTP Basic client_id:client_secret. Zoom documents PKCE for public clients that do NOT send the
  Authorization header, and combining the two is unverified to work live, so v1 omits it; CSRF and
  browser-binding protection come from the state parameter plus the nonce-bound FlowStore, same as the
  paste_back-capable providers get from state and nonce alone before PKCE is even checked.
* callback mode only. Zoom's own docs reserve loopback (paste_back) redirects for PKCE/native clients,
  not confidential clients like this one, so paste_back is refused with a fixed message rather than
  silently attempted. The authorize URL carries response_type=code, client_id, redirect_uri and state
  and no scope parameter: a Zoom General App's scopes are fixed in the Marketplace app configuration,
  not requested at authorize time.
* The client id/secret are read from the connection store (pipeline.connections), never the
  environment, mirroring oauth_gmail._client_credentials. The connection must be kind "zoom" and in
  auth_mode "oauth" (pipeline.connections.zoom_auth_mode): an s2s-configured connection has no browser
  flow to offer.
* finish_callback persists only the refresh token, via connections.set_secret("zoom", "refresh_token",
  ...); the access token is never stored. Every sign-in failure is a fixed-message FlowError subclass
  with no arguments and every exchange failure is raised `from None`, so a code, a client secret or a
  token cannot reach a message, a repr or a traceback by construction (same discipline as oauth_gmail
  and oauth_m365).
* refresh() and revoke() are a separate, non-FlowError surface: they are called from the health/collector
  path (T-005), not the browser sign-in flow, so a router `except FlowError` must not swallow them.
  RefreshTokenRejected (Zoom's invalid_grant / invalid_request-naming-the-token) is distinguished from
  RefreshFailed (anything else, including a response that is missing a token field) because only the
  former means the stored refresh token is dead and the connection needs a fresh sign-in; Zoom always
  rotates the refresh token on use, so a response without one cannot be persisted safely either way.
  revoke() is best-effort cleanup and never raises; nothing wires it yet.
"""
import secrets
from typing import NoReturn, Optional
from urllib.parse import urlencode

import requests

from pipeline import connections, oauth_gmail
from pipeline.oauth_flows import MODES, FlowStore, PendingFlow, build_callback_url

PROVIDER = "zoom"
AUTHORIZE_ENDPOINT = "https://zoom.us/oauth/authorize"
TOKEN_ENDPOINT = "https://zoom.us/oauth/token"
REVOKE_ENDPOINT = "https://zoom.us/oauth/revoke"

_CALLBACK_INSTRUCTIONS = "Open the link and approve Zoom access. You will be returned here automatically."

StartResult = oauth_gmail.StartResult


class PasteBackNotSupported(oauth_gmail.InvalidMode):
    """Zoom's loopback redirect is for PKCE/native clients only; this is a confidential client."""

    message = "Zoom sign-in supports callback mode only"


class UnknownConnection(oauth_gmail.UnknownConnection):
    """No Zoom connection has this id (or it is not a Zoom connection)."""

    message = "no such Zoom connection"


class NotOAuthConfigured(UnknownConnection):
    """The Zoom connection is set up for server-to-server auth, so there is no browser flow for it."""

    message = "this Zoom connection is configured for server-to-server auth, not OAuth sign-in"


class ClientCredentialsMissing(oauth_gmail.ClientCredentialsMissing):
    """The connection lacks the OAuth client id or client secret the consent and exchange need."""

    message = "the Zoom connection needs both a client id and a client secret before signing in"


class CallbackUnavailable(oauth_gmail.CallbackUnavailable):
    """Callback mode needs a configured PUBLIC_BASE_URL, and Zoom offers no other mode."""

    message = "Zoom sign-in needs PUBLIC_BASE_URL set to an https address"


class ConsentDenied(oauth_gmail.ConsentDenied):
    """The user declined (or cancelled) on Zoom's consent screen."""

    message = "Zoom sign-in was cancelled or denied"


class ProviderError(oauth_gmail.ProviderError):
    """Zoom answered with an error other than a denial, or with no code."""

    message = "Zoom reported an error during sign-in; start again"


class ExchangeFailed(oauth_gmail.ExchangeFailed):
    """The token endpoint rejected the code or could not be reached."""

    message = "Zoom did not accept the sign-in code (expired, already used, or unreachable); start again"


class NoRefreshToken(oauth_gmail.NoRefreshToken):
    """Zoom issued no refresh token."""

    message = "Zoom returned no refresh token; remove this app under your Zoom account and start again"


class RefreshTokenRejected(Exception):
    """
    Zoom's token endpoint rejected the stored refresh token (invalid_grant, or invalid_request naming
    the token): it is expired, revoked or already rotated by an earlier call. Not a FlowError: this is
    raised from the health/collector refresh path (T-005), not the browser sign-in flow.
    """

    message = "Zoom rejected the stored refresh token; the connection needs to be signed in again"

    def __init__(self) -> None:
        super().__init__(self.message)


class RefreshFailed(Exception):
    """Any other refresh failure: network error, an unexpected response, or a response missing a required token field."""

    message = "Zoom token refresh failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class TokenExchangeError(Exception):
    """Internal: the token endpoint rejected the code exchange or could not be reached. Caught by finish()."""


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """
    Build the Zoom consent URL.

    No scope parameter: a Zoom General App's scopes come from the Marketplace app configuration, not
    the authorize request.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """
    Exchange the authorization code for tokens (HTTP Basic client_id:client_secret).

    :returns: the token endpoint's JSON body.
    :raises TokenExchangeError: network failure, non-200, or a body that is not a JSON object.
    """
    try:
        resp = requests.post(
            TOKEN_ENDPOINT,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            auth=(client_id, client_secret),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise TokenExchangeError("token request failed") from exc
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise TokenExchangeError(f"token endpoint returned an unexpected body (HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise TokenExchangeError(f"token exchange rejected (HTTP {resp.status_code})")
    return body


def _view_and_credentials(connection_id: str) -> tuple[connections.ConnectionView, str, str]:
    view = connections.get(connection_id)
    if view is None or view.kind != "zoom":
        raise UnknownConnection()
    if connections.zoom_auth_mode(view) != "oauth":
        raise NotOAuthConfigured()
    client_id = view.config.get("client_id")
    client_secret = connections.get_secret(connection_id, "client_secret")
    if not isinstance(client_id, str) or not client_id or not client_secret:
        raise ClientCredentialsMissing()
    return view, client_id, client_secret


def start(
    connection_id: str,
    mode: str,
    *,
    store: FlowStore,
    public_base_url: Optional[str] = None,
) -> StartResult:
    """
    Register a pending sign-in and build the Zoom consent URL for it.

    :param mode: must be "callback"; Zoom offers no paste_back flow.
    :param public_base_url: the configured PUBLIC_BASE_URL; required.
    :raises PasteBackNotSupported: mode is "paste_back".
    :raises oauth_gmail.InvalidMode: mode is neither paste_back nor callback.
    :raises UnknownConnection: no Zoom connection with this id.
    :raises NotOAuthConfigured: the connection's auth_mode is s2s, not oauth.
    :raises ClientCredentialsMissing: the connection has no client id or no client secret.
    :raises CallbackUnavailable: no public_base_url configured.
    :raises InvalidCallbackConfig: (pipeline.oauth_flows) public_base_url is not an https URL.
    :raises SecretKeyMissing: no vault installed; SecretDecryptError: stored secrets cannot be opened.
    """
    if mode not in MODES:
        raise oauth_gmail.InvalidMode()
    if mode == "paste_back":
        raise PasteBackNotSupported()
    _, client_id, _ = _view_and_credentials(connection_id)
    if public_base_url is None:
        raise CallbackUnavailable()
    redirect_uri = build_callback_url(public_base_url, PROVIDER)

    state = secrets.token_urlsafe(32)
    flow, nonce = store.create(PROVIDER, connection_id, mode, state, redirect_uri, {})
    return StartResult(
        flow_id=flow.flow_id,
        auth_url=build_auth_url(client_id, redirect_uri, state),
        mode=mode,
        expires_at=flow.expires_at,
        nonce=nonce,
        instructions=_CALLBACK_INSTRUCTIONS,
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
    :raises NotOAuthConfigured: the connection's auth_mode changed to s2s mid-flow.
    :raises ClientCredentialsMissing: the client id or secret was removed mid-flow.
    :raises ProviderError: no code.
    :raises ExchangeFailed: the token endpoint rejected the code or was unreachable.
    :raises NoRefreshToken: the response carried no refresh token.
    :raises ConnectionError: (pipeline.connections) if the refresh token cannot be stored.
    """
    if not isinstance(code, str) or not code:
        raise ProviderError()
    _, client_id, client_secret = _view_and_credentials(flow.connection_id)
    try:
        tokens = exchange_code(client_id, client_secret, code, flow.redirect_uri)
    except TokenExchangeError:
        # from None: the original message can carry Zoom's error text and the request's URL.
        raise ExchangeFailed() from None
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise NoRefreshToken()
    return connections.set_secret(flow.connection_id, "refresh_token", refresh_token)


def finish_callback(
    store: FlowStore,
    state: Optional[str],
    code: Optional[str],
    nonce: Optional[str],
    error: Optional[str] = None,
) -> connections.ConnectionView:
    """
    Finish a callback sign-in from the query parameters Zoom sent to /api/oauth/callback/zoom.

    :param error: Zoom's `error` parameter, when the user denied consent.
    :raises UnknownFlow, ExpiredFlow, NonceMismatch, ProviderMismatch: (pipeline.oauth_flows) from the
        store; the flow is looked up by state, and only the browser holding the nonce can consume it.
    :raises ConsentDenied, ProviderError: `error` was present.
    :raises: everything finish() raises.
    """
    flow = store.consume(state=state, nonce=nonce, provider=PROVIDER)
    if error:
        _raise_provider_error(error)
    return finish(flow, code or "")


def refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """
    Rotate a Zoom refresh token.

    :returns: the token endpoint's JSON body (access_token, refresh_token, expires_in, ...).
    :raises RefreshTokenRejected: Zoom reports invalid_grant, or invalid_request naming the token; the
        stored refresh token can no longer be used and the connection needs to be signed in again.
    :raises RefreshFailed: any other failure, an unreadable response, or a response missing
        access_token, refresh_token or expires_in (Zoom always rotates, so a missing refresh_token means
        we cannot persist and must not proceed).
    """
    try:
        resp = requests.post(
            TOKEN_ENDPOINT,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(client_id, client_secret),
            timeout=15,
        )
    except requests.RequestException:
        raise RefreshFailed() from None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code in (400, 401) and isinstance(body, dict):
        error = body.get("error")
        reason = f"{body.get('reason') or ''} {body.get('error_description') or ''}".lower()
        if error == "invalid_grant" or (error == "invalid_request" and "token" in reason):
            raise RefreshTokenRejected() from None
        raise RefreshFailed()
    if resp.status_code != 200 or not isinstance(body, dict):
        raise RefreshFailed()
    if not body.get("access_token") or not body.get("refresh_token") or "expires_in" not in body:
        raise RefreshFailed()
    return body


def revoke(client_id: str, client_secret: str, token: str) -> bool:
    """
    Best-effort revoke of a Zoom token (access or refresh). Never raises: this is cleanup, not part of
    the sign-in or refresh critical path, and nothing wires it yet.

    :returns: True if Zoom answered 200, False otherwise (including a network failure).
    """
    try:
        resp = requests.post(REVOKE_ENDPOINT, params={"token": token}, auth=(client_id, client_secret), timeout=15)
    except requests.RequestException:
        return False
    return resp.status_code == 200
