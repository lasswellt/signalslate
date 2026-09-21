"""
One-time interactive sign-in (auth-code + PKCE, loopback redirect) for one Gmail mailbox label.
Prints GMAIL_<LABEL>_REFRESH_TOKEN=... for you to paste into .env; it never writes the token to a file.

Uses a Google "Desktop app" OAuth client (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env) and only the
gmail.readonly scope. Stdlib http.server + requests instead of google-auth-oauthlib: the whole flow is
one redirect and one POST, not worth a dependency.

Prerequisites, because both fail silently later:
  - The OAuth consent screen must be published to "In production". In Testing status Google expires
    refresh tokens after 7 days.
  - Google keeps at most 100 refresh tokens per account per client and silently invalidates the oldest,
    and every run of this script mints one. Do not re-run casually.

Headless box: pass --no-browser to print the URL and --port N, then forward that port over SSH
(ssh -L N:127.0.0.1:N <host>) and open the URL on your laptop. (Implementation note, not a documented
Google recipe.)

Re-run when the dashboard shows invalid_grant for this mailbox (consent revoked, password change on a
Workspace-managed account, token evicted by the 100-token cap).

Usage:
    python auth/gmail_bootstrap.py personal
    python auth/gmail_bootstrap.py personal --force        # mint a new token even if the current one works
    python auth/gmail_bootstrap.py personal --port 8765 --no-browser
"""
import argparse
import base64
import hashlib
import hmac
import re
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.health import GMAIL_SCOPE, _env, check_gmail, gmail_accounts  # noqa: E402

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
LOOPBACK_HOST = "127.0.0.1"  # never 0.0.0.0: the auth code must not be reachable from the LAN
DEFAULT_TIMEOUT = 300
_LABEL = re.compile(r"[A-Za-z0-9]+")


class BootstrapError(Exception):
    """Base for every expected failure of the sign-in; main() turns these into a non-zero exit."""


class StateMismatchError(BootstrapError):
    """The redirect's state did not match ours: not our request, or a forged one."""


class AuthorizationError(BootstrapError):
    """Google redirected back with error= (e.g. access_denied)."""


class ListenerTimeoutError(BootstrapError):
    """No redirect arrived before the deadline."""


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


def build_auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    """
    Build the Google consent URL.

    access_type=offline + prompt=consent are both required: without them Google omits the refresh
    token on any repeat sign-in of the same account/client.
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
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def check_state(expected: str, received: Optional[str]) -> None:
    """
    :raises StateMismatchError: if received is missing or differs from expected.
    """
    # Constant-time compare; the value is short-lived but there is no reason to leak it byte by byte.
    if not received or not hmac.compare_digest(expected.encode(), received.encode()):
        raise StateMismatchError("state mismatch on the redirect; not continuing (possible forged callback)")


def wait_for_code(
    port: int,
    state: str,
    timeout: float = DEFAULT_TIMEOUT,
    on_bound: Optional[Callable[[int], None]] = None,
) -> str:
    """
    Serve the OAuth redirect on 127.0.0.1 until one callback arrives, and return its auth code.

    The listener is bound before on_bound(actual_port) fires so the caller can build a redirect URI
    with the port that is really listening (port=0 lets the OS pick a free one). Requests to paths
    other than "/" (a browser's favicon fetch) are answered 404 and do not end the wait.

    :param port: port to bind on 127.0.0.1; 0 for a random free port
    :param state: the state value sent in the auth URL; the callback must echo it
    :param timeout: seconds to wait for the callback
    :param on_bound: called once with the bound port, before waiting
    :returns: the authorization code
    :raises StateMismatchError: callback state missing or wrong
    :raises AuthorizationError: callback carried error= (user denied consent, etc.)
    :raises ListenerTimeoutError: no callback before the deadline
    """
    outcome: dict[str, Optional[str]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
            parts = urlsplit(self.path)
            if parts.path != "/":
                self._reply(404, "Not found.")
                return
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            try:
                check_state(state, query.get("state"))
            except StateMismatchError as exc:
                outcome["error"] = "state"
                outcome["detail"] = str(exc)
                self._reply(400, "State mismatch. You can close this tab; nothing was authorized.")
                return
            if "error" in query:
                outcome["error"] = "authorization"
                outcome["detail"] = query["error"]
                self._reply(400, f"Authorization failed: {query['error']}. You can close this tab.")
                return
            if not query.get("code"):
                outcome["error"] = "authorization"
                outcome["detail"] = "redirect carried no code"
                self._reply(400, "No authorization code received. You can close this tab.")
                return
            outcome["code"] = query["code"]
            self._reply(200, "Signed in. You can close this tab and return to the terminal.")

        def _reply(self, status: int, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            # Default logging would print the request line, which contains the auth code.
            return

    try:
        server = HTTPServer((LOOPBACK_HOST, port), Handler)
    except OSError as exc:
        raise BootstrapError(f"cannot listen on {LOOPBACK_HOST}:{port}: {exc}") from exc
    try:
        if on_bound is not None:
            on_bound(server.server_address[1])
        deadline = time.monotonic() + timeout
        while not outcome:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ListenerTimeoutError(f"no redirect received within {timeout:g}s")
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()

    if "code" in outcome:
        return outcome["code"]  # type: ignore[return-value]
    if outcome["error"] == "state":
        raise StateMismatchError(outcome["detail"] or "state mismatch")
    raise AuthorizationError(f"Google returned error={outcome['detail']}")


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str, redirect_uri: str) -> dict:
    """
    Exchange the auth code (plus PKCE verifier) for tokens.

    Desktop clients still send client_secret; Google does not treat it as confidential there but
    the token endpoint requires it.

    :returns: the token endpoint's JSON body
    :raises TokenExchangeError: network failure, non-200, or a non-JSON body
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
        body = {}
    if resp.status_code != 200:
        error = body.get("error", f"http_{resp.status_code}")
        description = body.get("error_description", "")
        raise TokenExchangeError(f"token exchange rejected: {error}" + (f": {description}" if description else ""))
    if not isinstance(body, dict):
        raise TokenExchangeError("token endpoint returned an unexpected body")
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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive (browser, loopback + PKCE) sign-in for one Gmail label")
    parser.add_argument("label", help="mailbox label, letters/digits only, e.g. personal; becomes GMAIL_<LABEL>_REFRESH_TOKEN")
    parser.add_argument("--force", action="store_true", help="mint a new token even if the configured one is healthy")
    parser.add_argument("--port", type=int, default=0, help="loopback port to listen on (default: random free port)")
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    args = parser.parse_args(argv)

    if not _LABEL.fullmatch(args.label):
        print("label must match [A-Za-z0-9]+", file=sys.stderr)
        return 2
    if not 0 <= args.port <= 65535:
        print("--port must be 0-65535", file=sys.stderr)
        return 2
    label = args.label.lower()

    if gmail_accounts().get(label) and not args.force:
        result = check_gmail(label)
        print(f"[{label}] {result.status.upper()} — {result.detail}")
        if result.status == "ok":
            return 0
        print(f"[{label}] configured token is not usable; signing in again")

    env = _env()
    client_id = env.get("GMAIL_CLIENT_ID")
    client_secret = env.get("GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Missing GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env (Google Cloud Console > Credentials > Desktop app)", file=sys.stderr)
        return 1

    print("Before you continue:")
    print('  - The OAuth consent screen must be published to "In production"; in Testing, refresh tokens die after 7 days.')
    print("  - Google keeps at most 100 refresh tokens per account per client (oldest silently invalidated).")
    print("    Every run of this script mints one, so do not re-run casually.")

    state = secrets.token_urlsafe(32)
    verifier, challenge = make_pkce()
    redirect: dict[str, str] = {}

    def announce(bound_port: int) -> None:
        redirect["uri"] = f"http://{LOOPBACK_HOST}:{bound_port}"
        url = build_auth_url(client_id, redirect["uri"], challenge, state)
        if args.no_browser:
            print(f"[{label}] open this URL in a browser that can reach 127.0.0.1:{bound_port}:\n{url}")
        else:
            print(f"[{label}] opening browser (listening on {redirect['uri']}); if nothing opens, visit:\n{url}")
            # open_new_tab is fire-and-forget; a False return just means the user uses the printed URL.
            threading.Thread(target=webbrowser.open_new_tab, args=(url,), daemon=True).start()

    try:
        code = wait_for_code(args.port, state, DEFAULT_TIMEOUT, on_bound=announce)
        tokens = exchange_code(client_id, client_secret, code, verifier, redirect["uri"])
        refresh_token = assert_granted(tokens)
    except BootstrapError as exc:
        print(f"[{label}] FAILED — {exc}", file=sys.stderr)
        return 1

    print(f"[{label}] OK — refresh token minted for scope {GMAIL_SCOPE}")
    print("Add this line to .env (on the server), then re-run the health check:")
    print(f"GMAIL_{label.upper()}_REFRESH_TOKEN={refresh_token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
