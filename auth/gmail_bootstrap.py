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
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional, Type
from urllib.parse import parse_qs, urlsplit

import requests  # noqa: F401  (kept importable as gmail_bootstrap.requests: tests patch gb.requests.post)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.health import GMAIL_SCOPE, _env, check_gmail, gmail_accounts  # noqa: E402
# The pure helpers live in pipeline/ so the API never imports this CLI; re-exported under the same names.
from pipeline.oauth_google import (  # noqa: E402,F401
    AUTH_ENDPOINT,
    TOKEN_ENDPOINT,
    BootstrapError,
    GrantError,
    StateMismatchError,
    TokenExchangeError,
    assert_granted,
    build_auth_url,
    check_state,
    exchange_code,
    make_pkce,
)

LOOPBACK_HOST = "127.0.0.1"  # never 0.0.0.0: the auth code must not be reachable from the LAN
DEFAULT_TIMEOUT = 300
# Per accepted socket. Measured: on a single-threaded server an idle connection (a browser's speculative
# preconnect, any local process) stalled the accept loop long past the deadline. Long enough for a slow
# browser to send its request line, short enough that a dead connection frees its thread quickly.
CONNECTION_TIMEOUT = 5
_LABEL = re.compile(r"[A-Za-z0-9]+")


class AuthorizationError(BootstrapError):
    """Google redirected back with error= (e.g. access_denied)."""


class ListenerTimeoutError(BootstrapError):
    """No redirect arrived before the deadline."""


class _ListenerServer(ThreadingHTTPServer):
    # One thread per connection so an idle one never blocks the accept loop; daemon so it can never
    # keep the process alive or be joined by server_close().
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # The default prints a traceback to stderr for every socket timeout/reset; this CLI's output must
        # stay clean. Anything that is not a socket error still gets the default treatment.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)


def bind_listener(port: int, handler_cls: Type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    """
    Bind the callback listener to LOOPBACK_HOST only (listening, not yet serving).

    :param port: port to bind; 0 for a random free port
    :param handler_cls: request handler class
    :returns: the bound server; the caller must server_close() it
    :raises BootstrapError: the address cannot be bound
    """
    try:
        return _ListenerServer((LOOPBACK_HOST, port), handler_cls)
    except OSError as exc:
        raise BootstrapError(f"cannot listen on {LOOPBACK_HOST}:{port}: {exc}") from exc


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
    outcome_lock = threading.Lock()
    done = threading.Event()

    def claim(**entries: str) -> bool:
        # Handlers run on their own threads; the first decisive callback wins and later ones cannot
        # overwrite a recorded code or error.
        with outcome_lock:
            if outcome:
                return False
            outcome.update(entries)
            return True

    class Handler(BaseHTTPRequestHandler):
        timeout = CONNECTION_TIMEOUT

        def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
            parts = urlsplit(self.path)
            if parts.path != "/":
                self._reply(404, "Not found.")
                return
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            try:
                check_state(state, query.get("state"))
            except StateMismatchError as exc:
                status, body = 400, "State mismatch. You can close this tab; nothing was authorized."
                won = claim(error="state", detail=str(exc))
            else:
                if "error" in query:
                    status, body = 400, f"Authorization failed: {query['error']}. You can close this tab."
                    won = claim(error="authorization", detail=query["error"])
                elif not query.get("code"):
                    status, body = 400, "No authorization code received. You can close this tab."
                    won = claim(error="authorization", detail="redirect carried no code")
                else:
                    status, body = 200, "Signed in. You can close this tab and return to the terminal."
                    won = claim(code=query["code"])
            if not won:
                status, body = 409, "A callback was already received. You can close this tab."
            try:
                self._reply(status, body)
            finally:
                # Only the winner ends the wait, and only once its reply is flushed, so the browser sees
                # the result page before wait_for_code returns and the process can exit.
                if won:
                    done.set()

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

    server = bind_listener(port, Handler)
    serving: Optional[threading.Thread] = None
    try:
        if on_bound is not None:
            on_bound(server.server_address[1])
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        serving.start()
        done.wait(timeout)
        with outcome_lock:
            decided = dict(outcome)
        if not decided:
            raise ListenerTimeoutError(f"no redirect received within {timeout:g}s")
    finally:
        # shutdown() blocks until serve_forever exits, so it is only safe once that thread was started.
        if serving is not None:
            server.shutdown()
            serving.join()
        server.server_close()

    if "code" in decided:
        return decided["code"]  # type: ignore[return-value]
    if decided["error"] == "state":
        raise StateMismatchError(decided["detail"] or "state mismatch")
    raise AuthorizationError(f"Google returned error={decided['detail']}")


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
