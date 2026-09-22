"""
pipeline.oauth_google is the API-safe home of the helpers auth/gmail_bootstrap.py used to define.
tests/test_gmail_bootstrap.py still exercises them through the CLI's re-exports; these tests import
from the pipeline module directly and pin what the move must not change.
"""
import base64
import hashlib
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from pipeline import oauth_google as og
from pipeline.health import GMAIL_SCOPE

ROOT = Path(__file__).resolve().parent.parent


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_importing_oauth_google_does_not_import_the_cli():
    code = "import sys, pipeline.oauth_google; sys.exit(1 if 'auth.gmail_bootstrap' in sys.modules else 0)"
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_cli_reexports_are_the_same_objects():
    from auth import gmail_bootstrap as gb

    for name in (
        "make_pkce", "build_auth_url", "check_state", "exchange_code", "assert_granted",
        "AUTH_ENDPOINT", "TOKEN_ENDPOINT", "BootstrapError", "StateMismatchError", "TokenExchangeError", "GrantError",
    ):
        assert getattr(gb, name) is getattr(og, name), name
    assert issubclass(gb.AuthorizationError, og.BootstrapError)
    assert issubclass(gb.ListenerTimeoutError, og.BootstrapError)


def test_make_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = og.make_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert 43 <= len(verifier) <= 128


def test_build_auth_url_without_login_hint_is_the_historical_url():
    url = og.build_auth_url("cid.apps.example", "http://127.0.0.1:5555", "chal", "st8")
    assert url == (
        "https://accounts.google.com/o/oauth2/v2/auth"
        "?client_id=cid.apps.example"
        "&redirect_uri=http%3A%2F%2F127.0.0.1%3A5555"
        "&response_type=code"
        "&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.readonly"
        "&access_type=offline&prompt=consent"
        "&code_challenge=chal&code_challenge_method=S256"
        "&state=st8"
    )


def test_build_auth_url_none_login_hint_equals_omitted():
    args = ("cid", "http://127.0.0.1:1", "chal", "st8")
    assert og.build_auth_url(*args, login_hint=None) == og.build_auth_url(*args)


def test_build_auth_url_adds_login_hint_when_given():
    args = ("cid", "http://127.0.0.1:1", "chal", "st8")
    url = og.build_auth_url(*args, login_hint="user@example.com")
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["login_hint"] == "user@example.com"
    assert url.startswith(og.build_auth_url(*args) + "&login_hint=")
    assert {k: v for k, v in q.items() if k != "login_hint"} == {
        k: v[0] for k, v in parse_qs(urlsplit(og.build_auth_url(*args)).query).items()
    }


def test_check_state_accepts_match_and_rejects_missing_or_wrong():
    og.check_state("abc", "abc")
    for received in (None, "", "abd"):
        with pytest.raises(og.StateMismatchError):
            og.check_state("abc", received)


def test_exchange_code_posts_authorization_code_grant(monkeypatch):
    seen = {}

    def fake_post(url, data, timeout):
        seen.update(url=url, data=data)
        return _FakeResponse(200, {"refresh_token": "rt", "scope": GMAIL_SCOPE})

    monkeypatch.setattr(requests, "post", fake_post)
    out = og.exchange_code("cid", "sec", "the-code", "the-verifier", "http://127.0.0.1:1234")
    assert out["refresh_token"] == "rt"
    assert seen["url"] == og.TOKEN_ENDPOINT
    assert seen["data"]["grant_type"] == "authorization_code"
    assert seen["data"]["code_verifier"] == "the-verifier"


def test_exchange_code_rejection_names_google_error_and_never_echoes_secrets(monkeypatch):
    body = {"error": "invalid_grant", "error_description": "Bad Request"}
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(400, body))
    with pytest.raises(og.TokenExchangeError, match="invalid_grant: Bad Request") as exc:
        og.exchange_code("cid", "client-secret-xyz", "auth-code-xyz", "verifier-xyz", "http://127.0.0.1:1")
    for secret in ("client-secret-xyz", "auth-code-xyz", "verifier-xyz"):
        assert secret not in str(exc.value)


def test_exchange_code_wraps_network_failure(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(og.TokenExchangeError, match="token request failed"):
        og.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")


def test_exchange_code_non_dict_body_names_only_the_status(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(200, ["refresh_token-xyz"]))
    with pytest.raises(og.TokenExchangeError, match=r"HTTP 200") as exc:
        og.exchange_code("cid", "sec", "c", "v", "http://127.0.0.1:1")
    assert "refresh_token-xyz" not in str(exc.value)


def test_assert_granted_returns_refresh_token_and_rejects_bad_grants():
    assert og.assert_granted({"refresh_token": "rt", "scope": f"openid {GMAIL_SCOPE}"}) == "rt"
    with pytest.raises(og.GrantError, match="granted scopes"):
        og.assert_granted({"refresh_token": "rt", "scope": "openid"})
    with pytest.raises(og.GrantError, match="no refresh_token"):
        og.assert_granted({"scope": GMAIL_SCOPE})
