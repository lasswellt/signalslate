"""
Pure Google OAuth (auth-code + PKCE) helpers shared by the CLI (auth/gmail_bootstrap.py) and the API.

Lives in pipeline/ because the API must not import a CLI module (docs/_research/2026-09-21_management-ui.md).
Nothing here binds a socket, prints, opens a browser or touches the environment: the loopback listener
and all terminal I/O stay in the CLI. auth/gmail_bootstrap.py re-exports these names, so
`gmail_bootstrap.make_pkce` and friends keep working for existing callers.

Secret discipline: an auth code, verifier or client secret never goes into an exception message; the
token endpoint's body is echoed only through _bounded_text, and only its error fields.
"""
import base64
import hashlib
import hmac
import re
import secrets
from typing import Optional
from urllib.parse import urlencode

import requests

from pipeline.health import GMAIL_SCOPE

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


class BootstrapError(Exception):
    """Base for every expected failure of the sign-in; main() turns these into a non-zero exit."""


class StateMismatchError(BootstrapError):
    """The redirect's state did not match ours: not our request, or a forged one."""


class TokenExchangeError(BootstrapError):
    """The token endpoint rejected the code or could not be reached."""


class GrantError(BootstrapError):
    """The exchange succeeded but the grant is unusable (no refresh token, or scope not granted)."""


def make_pkce() -> tuple[str, str]:
    """
    Generate a PKCE pair.

    :returns: (verifier, challenge); challenge = base64url(sha256(verifier)) without padding (S256).
    """
    # 64 random bytes -> 86 url-safe chars, inside RFC 7636's 43-128 range.
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_auth_url(
    client_id: str,
    redirect_uri: str,
    challenge: str,
    state: str,
    login_hint: Optional[str] = None,
) -> str:
    """
    Build the Google consent URL.

    access_type=offline + prompt=consent are both required: without them Google omits the refresh
    token on any repeat sign-in of the same account/client.

    :param login_hint: optional account to preselect on the consent screen; appended last so the URL
        is byte-for-byte the historical one when it is absent
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if login_hint is not None:
        params["login_hint"] = login_hint
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def check_state(expected: str, received: Optional[str]) -> None:
    """
    :raises StateMismatchError: if received is missing or differs from expected.
    """
    # Constant-time compare; the value is short-lived but there is no reason to leak it byte by byte.
    if not received or not hmac.compare_digest(expected.encode(), received.encode()):
        raise StateMismatchError("state mismatch on the redirect; not continuing (possible forged callback)")


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_ERROR_TEXT = 200


def _bounded_text(value: object) -> str:
    """
    Render an untrusted JSON value for an error message: str(), control characters removed (no
    terminal escape or log-line injection), capped so a hostile body cannot flood the terminal.
    """
    if value is None:
        return ""
    return _CONTROL_CHARS.sub("", str(value))[:_MAX_ERROR_TEXT]


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str, redirect_uri: str) -> dict:
    """
    Exchange the auth code (plus PKCE verifier) for tokens.

    Desktop clients still send client_secret; Google does not treat it as confidential there but
    the token endpoint requires it.

    :returns: the token endpoint's JSON body
    :raises TokenExchangeError: network failure, non-200, or a body that is not a JSON object
    """
    try:
        resp = requests.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise TokenExchangeError(f"token request failed: {exc}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        # Name the status only: an unexpected body is not ours to echo, it may carry secrets.
        raise TokenExchangeError(f"token endpoint returned an unexpected body (HTTP {resp.status_code})")
    if resp.status_code != 200:
        error = _bounded_text(body.get("error")) or f"http_{resp.status_code}"
        description = _bounded_text(body.get("error_description"))
        raise TokenExchangeError(f"token exchange rejected: {error}" + (f": {description}" if description else ""))
    return body


def assert_granted(token_response: dict) -> str:
    """
    :returns: the refresh token
    :raises GrantError: no refresh_token, or GMAIL_SCOPE missing from the space-delimited scope field
    """
    granted = set((token_response.get("scope") or "").split())
    if GMAIL_SCOPE not in granted:
        raise GrantError(f"granted scopes do not include {GMAIL_SCOPE}; tick the Gmail read-only box on the consent screen")
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        raise GrantError(
            "no refresh_token in the response; revoke this app at https://myaccount.google.com/permissions and re-run"
        )
    return refresh_token
