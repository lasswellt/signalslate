import base64
import hashlib
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest  # pyright: ignore[reportMissingImports]  (checker runs outside the venv)
import requests

from auth import gmail_bootstrap as gb
from pipeline.health import GMAIL_SCOPE

ROOT = Path(__file__).resolve().parent.parent


def _serve(state, timeout=5):
    """Run wait_for_code on a random port in a thread; returns (port, result dict, thread)."""
    bound = threading.Event()
    box: dict = {}
    result: dict = {}

    def on_bound(port):
        box["port"] = port
        bound.set()

    def run():
        try:
            result["code"] = gb.wait_for_code(0, state, timeout, on_bound=on_bound)
        except gb.BootstrapError as exc:
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    assert bound.wait(5), "listener never bound"
    return box["port"], result, thread


def test_make_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = gb.make_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge


def test_make_pkce_verifier_length_and_uniqueness():
    a, _ = gb.make_pkce()
    b, _ = gb.make_pkce()
    assert 43 <= len(a) <= 128
    assert a != b


def test_build_auth_url_carries_required_params():
    url = gb.build_auth_url("cid.apps.example", "http://127.0.0.1:5555", "chal", "st8")
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert q == {
        "client_id": "cid.apps.example",
        "redirect_uri": "http://127.0.0.1:5555",
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": "chal",
        "code_challenge_method": "S256",
        "state": "st8",
    }


def test_check_state_accepts_match_and_rejects_others():
    gb.check_state("abc", "abc")
    with pytest.raises(gb.StateMismatchError):
        gb.check_state("abc", "abd")
    with pytest.raises(gb.StateMismatchError):
        gb.check_state("abc", None)
    with pytest.raises(gb.StateMismatchError):
        gb.check_state("abc", "")


def test_assert_granted_returns_refresh_token():
    resp = {"refresh_token": "rt-1", "scope": f"openid {GMAIL_SCOPE}"}
    assert gb.assert_granted(resp) == "rt-1"


def test_assert_granted_rejects_missing_refresh_token():
    with pytest.raises(gb.GrantError, match="refresh_token"):
        gb.assert_granted({"scope": GMAIL_SCOPE})


def test_assert_granted_rejects_missing_scope():
    with pytest.raises(gb.GrantError, match="scopes"):
        gb.assert_granted({"refresh_token": "rt", "scope": "openid email"})
    with pytest.raises(gb.GrantError):
        gb.assert_granted({"refresh_token": "rt"})


def test_wait_for_code_returns_code_on_good_state():
    port, result, thread = _serve("good-state")
    resp = requests.get(f"http://127.0.0.1:{port}/", params={"state": "good-state", "code": "auth-code"}, timeout=5)
    thread.join(5)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert result == {"code": "auth-code"}


def test_wait_for_code_rejects_bad_state():
    port, result, thread = _serve("good-state")
    resp = requests.get(f"http://127.0.0.1:{port}/", params={"state": "evil", "code": "stolen"}, timeout=5)
    thread.join(5)
    assert resp.status_code == 400
    assert isinstance(result.get("error"), gb.StateMismatchError)
    assert "code" not in result


def test_wait_for_code_surfaces_access_denied():
    port, result, thread = _serve("s")
    resp = requests.get(f"http://127.0.0.1:{port}/", params={"state": "s", "error": "access_denied"}, timeout=5)
    thread.join(5)
    assert resp.status_code == 400
    assert isinstance(result.get("error"), gb.AuthorizationError)
    assert "access_denied" in str(result["error"])


def test_wait_for_code_ignores_favicon_then_accepts_callback():
    port, result, thread = _serve("s")
    assert requests.get(f"http://127.0.0.1:{port}/favicon.ico", timeout=5).status_code == 404
    requests.get(f"http://127.0.0.1:{port}/", params={"state": "s", "code": "c"}, timeout=5)
    thread.join(5)
    assert result == {"code": "c"}


def test_wait_for_code_times_out():
    with pytest.raises(gb.ListenerTimeoutError):
        gb.wait_for_code(0, "s", timeout=0.3)


def _raw_get(sock, path):
    sock.sendall(f"GET {path} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
    chunks = []
    while chunk := sock.recv(4096):
        chunks.append(chunk)
    return b"".join(chunks)


def test_bind_listener_binds_loopback_only():
    from http.server import BaseHTTPRequestHandler

    assert gb.LOOPBACK_HOST == "127.0.0.1"
    server = gb.bind_listener(0, BaseHTTPRequestHandler)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_wait_for_code_times_out_at_deadline_with_idle_connection_open():
    started = time.monotonic()
    port, result, thread = _serve("s", timeout=1)
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        thread.join(5)
        elapsed = time.monotonic() - started
    assert not thread.is_alive()
    assert isinstance(result.get("error"), gb.ListenerTimeoutError)
    assert elapsed < 1 + 2


def test_wait_for_code_serves_callback_while_idle_connection_is_open():
    port, result, thread = _serve("s")
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        resp = requests.get(f"http://127.0.0.1:{port}/", params={"state": "s", "code": "c"}, timeout=5)
        thread.join(5)
    assert resp.status_code == 200
    assert result == {"code": "c"}


def test_wait_for_code_serves_callback_while_half_request_line_is_stalled():
    port, result, thread = _serve("s")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as stalled:
        stalled.sendall(b"GET /?sta")
        requests.get(f"http://127.0.0.1:{port}/", params={"state": "s", "code": "c"}, timeout=5)
        thread.join(5)
    assert result == {"code": "c"}


def test_wait_for_code_closes_idle_connection_after_connection_timeout(monkeypatch):
    monkeypatch.setattr(gb, "CONNECTION_TIMEOUT", 0.3)
    port, result, thread = _serve("s", timeout=4)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as idle:
            assert idle.recv(1) == b""
    finally:
        requests.get(f"http://127.0.0.1:{port}/", params={"state": "s", "code": "c"}, timeout=5)
        thread.join(5)
    assert result == {"code": "c"}


def test_wait_for_code_releases_port_after_timeout():
    from http.server import BaseHTTPRequestHandler

    bound: dict = {}
    with pytest.raises(gb.ListenerTimeoutError):
        gb.wait_for_code(0, "s", timeout=0.3, on_bound=lambda p: bound.setdefault("port", p))
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", bound["port"]), timeout=2)
    gb.bind_listener(bound["port"], BaseHTTPRequestHandler).server_close()


def test_wait_for_code_first_decisive_callback_wins():
    port, result, thread = _serve("s")
    # Connected before the valid callback, so the FIFO accept queue hands it to a handler thread first.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as late:
        requests.get(f"http://127.0.0.1:{port}/", params={"state": "s", "code": "c"}, timeout=5)
        thread.join(5)
        reply = _raw_get(late, "/?state=s&error=access_denied")
    assert reply.startswith(b"HTTP/1.0 409")
    assert result == {"code": "c"}


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_exchange_code_posts_authorization_code_grant(monkeypatch):
    seen = {}

    def fake_post(url, data, timeout):
        seen.update(url=url, data=data)
        return _FakeResponse(200, {"refresh_token": "rt", "scope": GMAIL_SCOPE})

    monkeypatch.setattr(gb.requests, "post", fake_post)
    out = gb.exchange_code("cid", "sec", "the-code", "the-verifier", "http://127.0.0.1:1234")
    assert out["refresh_token"] == "rt"
    assert seen["url"] == "https://oauth2.googleapis.com/token"
    assert seen["data"] == {
        "client_id": "cid",
        "client_secret": "sec",
        "code": "the-code",
        "code_verifier": "the-verifier",
        "grant_type": "authorization_code",
        "redirect_uri": "http://127.0.0.1:1234",
    }


def test_exchange_code_raises_with_google_error_on_non_200(monkeypatch):
    body = {"error": "invalid_grant", "error_description": "Bad Request"}
    monkeypatch.setattr(gb.requests, "post", lambda *a, **k: _FakeResponse(400, body))
    with pytest.raises(gb.TokenExchangeError, match="invalid_grant: Bad Request"):
        gb.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")


class _NonJsonResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


@pytest.mark.parametrize("body", [["x"], "just a string", 42, None])
def test_exchange_code_non_dict_json_body_on_400_raises_token_exchange_error(monkeypatch, body):
    monkeypatch.setattr(gb.requests, "post", lambda *a, **k: _FakeResponse(400, body))
    with pytest.raises(gb.TokenExchangeError, match="HTTP 400") as exc_info:
        gb.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")
    assert isinstance(exc_info.value, gb.BootstrapError)


@pytest.mark.parametrize("body", [["x"], "just a string", 42, None])
def test_exchange_code_non_dict_json_body_on_200_raises_token_exchange_error(monkeypatch, body):
    monkeypatch.setattr(gb.requests, "post", lambda *a, **k: _FakeResponse(200, body))
    with pytest.raises(gb.TokenExchangeError, match="HTTP 200"):
        gb.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")


def test_exchange_code_non_json_body_raises_token_exchange_error(monkeypatch):
    monkeypatch.setattr(gb.requests, "post", lambda *a, **k: _NonJsonResponse(400))
    with pytest.raises(gb.TokenExchangeError, match="HTTP 400") as exc_info:
        gb.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")
    assert isinstance(exc_info.value, gb.BootstrapError)


def test_exchange_code_empty_body_raises_token_exchange_error(monkeypatch):
    monkeypatch.setattr(gb.requests, "post", lambda *a, **k: _NonJsonResponse(502))
    with pytest.raises(gb.TokenExchangeError, match="HTTP 502"):
        gb.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")


@pytest.mark.parametrize("make_body", [
    lambda code, secret, verifier: [code, secret, verifier],
    lambda code, secret, verifier: f"echo {code} {secret} {verifier}",
])
def test_exchange_code_non_dict_body_never_echoes_request_secrets(monkeypatch, make_body):
    code, secret, verifier = "CODE-SENTINEL-1", "SECRET-SENTINEL-2", "VERIFIER-SENTINEL-3"
    monkeypatch.setattr(gb.requests, "post", lambda *a, **k: _FakeResponse(400, make_body(code, secret, verifier)))
    with pytest.raises(gb.TokenExchangeError) as exc_info:
        gb.exchange_code("cid", secret, code, verifier, "http://127.0.0.1:1")
    message = str(exc_info.value)
    for sentinel in (code, secret, verifier):
        assert sentinel not in message


@pytest.mark.parametrize("value", [{"nested": ["a", 1]}, ["a", "b"], 12345])
def test_exchange_code_coerces_non_string_error_fields(monkeypatch, value):
    body = {"error": value, "error_description": value}
    monkeypatch.setattr(gb.requests, "post", lambda *a, **k: _FakeResponse(400, body))
    with pytest.raises(gb.TokenExchangeError, match="token exchange rejected: ") as exc_info:
        gb.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")
    assert str(value) in str(exc_info.value)


def test_exchange_code_caps_and_strips_control_characters_in_error_fields(monkeypatch):
    body = {"error": "bad\x1b[31m\nline", "error_description": "x" * 5000}
    monkeypatch.setattr(gb.requests, "post", lambda *a, **k: _FakeResponse(400, body))
    with pytest.raises(gb.TokenExchangeError) as exc_info:
        gb.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")
    message = str(exc_info.value)
    assert message.startswith("token exchange rejected: bad[31mline: xxx")
    assert not any(ch in message for ch in "\x1b\n")
    assert len(message) < 500


def test_exchange_code_falls_back_to_http_status_when_error_field_absent(monkeypatch):
    monkeypatch.setattr(gb.requests, "post", lambda *a, **k: _FakeResponse(503, {}))
    with pytest.raises(gb.TokenExchangeError, match="token exchange rejected: http_503$"):
        gb.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")


def test_wait_for_code_never_prints_the_auth_code(capsys):
    recognizable = "RECOGNIZABLE-CODE-1234"
    port, result, thread = _serve("good-state", timeout=3)
    resp = requests.get(
        f"http://127.0.0.1:{port}/", params={"state": "good-state", "code": recognizable}, timeout=5
    )
    thread.join(5)
    captured = capsys.readouterr()
    assert resp.status_code == 200
    assert result == {"code": recognizable}
    assert recognizable not in captured.out
    assert recognizable not in captured.err


def test_exchange_code_wraps_network_failure(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(gb.requests, "post", boom)
    with pytest.raises(gb.TokenExchangeError, match="token request failed"):
        gb.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")


def test_cli_help_exits_zero_without_env():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "auth" / "gmail_bootstrap.py"), "--help"],
        capture_output=True, text=True, timeout=20, cwd="/",
    )
    assert proc.returncode == 0
    assert "--no-browser" in proc.stdout


def test_cli_rejects_bad_label():
    assert gb.main(["bad-label!"]) == 2
