"""
Browser sign-in for a WordPress.com connection: start the WordPress.com consent, then finish it from
either the pasted loopback URL or the provider's callback, mirroring pipeline.oauth_gmail /
pipeline.oauth_google (docs/_research/2026-09-21_domain-collector.md Q3, Finding 2).

Same public shape as pipeline.oauth_gmail (start, finish_paste, finish_callback) so the API and the
route layer can treat every provider alike; the provider-neutral error classes are imported from there
and the WordPress.com ones subclass them, so one `except FlowError` covers every provider while each
message names the right one.

WordPress.com's documented OAuth2 flow (developer.wordpress.com/docs/oauth2/) is a plain
authorization-code exchange: no PKCE, and only one scope (`global`) to request. The docs describe no
refresh token, so this module stores only the access_token; a lapsed token is not refreshed here, it
surfaces later as a connection health/AuthFailed state and the user signs in again.

Design decisions mirror oauth_gmail.py: paste_back uses a fixed loopback redirect (nothing has to
listen there, a Desktop-style redirect just needs the code to land in the address bar); callback needs
an https PUBLIC_BASE_URL and is refused without one; the redirect_uri is kept server-side in the
pending flow and the exchange always presents that stored value, never one parsed out of a pasted URL;
every failure is a FlowError subclass with a fixed message and no arguments, so a code, state or client
secret cannot reach an exception message by construction; only the access_token is persisted, through
connections.set_secret (encrypted).
"""
import hmac
import secrets
from typing import NoReturn, Optional
from urllib.parse import urlencode

import requests

from pipeline import connections, oauth_gmail
from pipeline.oauth_flows import (
    MODES,
    FlowError,
    FlowStore,
    PendingFlow,
    build_callback_url,
    parse_pasted_url,
)

PROVIDER = "wordpress"
SCOPE = "global"
AUTH_ENDPOINT = "https://public-api.wordpress.com/oauth2/authorize"
TOKEN_ENDPOINT = "https://public-api.wordpress.com/oauth2/token"
# Nothing listens on this port and nothing has to: the browser's "cannot connect" page keeps the code
# in the address bar and the user pastes it back. A different port than oauth_gmail's so both flows can
# be started at once without one paste landing on the wrong provider's listener expectations (moot here
# since nothing listens, but it keeps the two providers' pending flows visually distinct in logs).
PASTE_BACK_PORT = 8767
PASTE_BACK_REDIRECT_URI = f"http://127.0.0.1:{PASTE_BACK_PORT}"

_PASTE_BACK_INSTRUCTIONS = (
    "Open the link and approve access on WordPress.com. Your browser will then show a page that cannot "
    "be reached: that is expected. Copy the full address from the address bar and paste it here straight "
    "away."
)
_CALLBACK_INSTRUCTIONS = "Open the link and approve access on WordPress.com. You will be returned here automatically."

InvalidMode = oauth_gmail.InvalidMode
CallbackUnavailable = oauth_gmail.CallbackUnavailable
StartResult = oauth_gmail.StartResult


class UnknownConnection(oauth_gmail.UnknownConnection):
    """No WordPress.com connection has this id (or it is not a wordpress connection)."""

    message = "no such WordPress.com connection"


class ClientCredentialsMissing(oauth_gmail.ClientCredentialsMissing):
    """The connection lacks the OAuth client id or client secret the consent and exchange need."""

    message = "the WordPress.com connection needs both a client id and a client secret before signing in"


class StateMismatch(oauth_gmail.StateMismatch):
    """The pasted URL answers a different sign-in than the one this browser started."""

    message = "the pasted URL does not belong to this sign-in; start again and paste the newest address"


class ConsentDenied(oauth_gmail.ConsentDenied):
    """The user declined (or cancelled) on WordPress.com's consent screen."""

    message = "WordPress.com sign-in was cancelled or denied"


class ProviderError(oauth_gmail.ProviderError):
    """WordPress.com answered with an error other than a denial, or with no code."""

    message = "WordPress.com reported an error during sign-in; start again"


class ExchangeFailed(oauth_gmail.ExchangeFailed):
    """The token endpoint rejected the code, returned no access token, or could not be reached."""

    message = "WordPress.com did not accept the sign-in code (expired, already used, or unreachable); start again"


def _client_credentials(connection_id: str) -> tuple[str, str]:
    view = connections.get(connection_id)
    if view is None or view.kind != "wordpress":
        raise UnknownConnection()
    client_id = view.config.get("client_id")
    client_secret = connections.get_secret(connection_id, "client_secret")
    if not isinstance(client_id, str) or not client_id or not client_secret:
        raise ClientCredentialsMissing()
    return client_id, client_secret


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the WordPress.com consent URL; `global` is the only documented scope."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def _check_state(expected: str, received: Optional[str]) -> None:
    if not received or not hmac.compare_digest(expected.encode(), received.encode()):
        raise StateMismatch()


def start(
    connection_id: str,
    mode: str,
    *,
    store: FlowStore,
    public_base_url: Optional[str] = None,
) -> StartResult:
    """
    Register a pending sign-in and build the WordPress.com consent URL for it.

    :param mode: "paste_back" (loopback redirect) or "callback" (https redirect on public_base_url).
    :param public_base_url: the configured PUBLIC_BASE_URL; required for callback.
    :raises InvalidMode: unknown mode.
    :raises UnknownConnection: no wordpress connection with this id.
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

    state = secrets.token_urlsafe(32)
    flow, nonce = store.create(PROVIDER, connection_id, mode, state, redirect_uri, {})
    return StartResult(
        flow_id=flow.flow_id,
        auth_url=build_auth_url(client_id, redirect_uri, state),
        mode=mode,
        expires_at=flow.expires_at,
        nonce=nonce,
        instructions=_PASTE_BACK_INSTRUCTIONS if mode == "paste_back" else _CALLBACK_INSTRUCTIONS,
    )


def _raise_provider_error(error: str) -> NoReturn:
    raise ConsentDenied() if error == "access_denied" else ProviderError()


def _exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> str:
    """
    :returns: the access_token.
    :raises ExchangeFailed: network failure, non-200, an unexpected body, or no access_token in it.
    """
    try:
        resp = requests.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
    except requests.RequestException:
        raise ExchangeFailed() from None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or resp.status_code != 200:
        raise ExchangeFailed()
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ExchangeFailed()
    return access_token


def finish(flow: PendingFlow, code: str) -> connections.ConnectionView:
    """
    Exchange the code for an access token and store it on the flow's connection.

    :param flow: a flow already consumed from the store (nonce and state verified by the caller).
    :param code: the authorization code; presented once and never retained.
    :returns: the connection's view (secret names only) after the access token is stored.
    :raises UnknownConnection: the connection was deleted mid-flow.
    :raises ClientCredentialsMissing: the client id or secret was removed mid-flow.
    :raises ProviderError: no code was presented.
    :raises ExchangeFailed: the token endpoint rejected the code, returned no access token, or was
        unreachable.
    :raises ConnectionError: (pipeline.connections) if the access token cannot be stored.
    """
    if not isinstance(code, str) or not code:
        raise ProviderError()
    client_id, client_secret = _client_credentials(flow.connection_id)
    access_token = _exchange_code(client_id, client_secret, code, flow.redirect_uri)
    return connections.set_secret(flow.connection_id, "access_token", access_token)


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
    _check_state(flow.state, parsed["state"])
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
    Finish a callback sign-in from the query parameters WordPress.com sent to
    /api/oauth/callback/wordpress.

    :param error: WordPress.com's `error` parameter, when the user denied consent.
    :raises UnknownFlow, ExpiredFlow, NonceMismatch, ProviderMismatch: from the store; the flow is looked
        up by state, and only the browser holding the nonce can consume it.
    :raises ConsentDenied, ProviderError: `error` was present.
    :raises: everything finish() raises.
    """
    flow = store.consume(state=state, nonce=nonce, provider=PROVIDER)
    if error:
        _raise_provider_error(error)
    return finish(flow, code or "")
