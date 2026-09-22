"""
Tests for the wordpress provider on api.routers.oauth, mirroring tests/test_api_oauth.py's setup. The
true external is WordPress.com's token endpoint (requests.post inside pipeline.oauth_wordpress); the
FlowStore, oauth_wordpress, the connection store, the vault and the request guard are all real.
"""
import logging
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import oauth as oauth_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import connections, crypto, db, health, oauth_wordpress  # noqa: E402
from pipeline.oauth_flows import FlowStore  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}
UI = "https://ui.example.com"
BASE_URL = "https://hook.example.com"
SUCCESS = f"{UI}/connections?oauth=ok"

WP_CLIENT_ID = "12345"
WP_CLIENT_SECRET = "wp-client-secret-Yn52kLp9Rq"
WP_ACCESS = "wp-access-token-Cx73mDs4Ez"
WP_ID = "wordpress_blog"
CODE = "wp-auth-code-Qb18zFj6Mo"
SECRETS = [WP_CLIENT_SECRET, WP_ACCESS, CODE]


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class TokenEndpoint:
    def __init__(self):
        self.calls: list[dict] = []
        self.reply = _FakeResponse(200, {"access_token": WP_ACCESS})


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def env(monkeypatch, tmp_path, temp_db):
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setenv("WEB_ORIGINS", UI)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    health.set_env_overlay_provider(connections.overlay_provider, connections.is_family_key)
    yield tmp_path
    connections.set_vault(None)


@pytest.fixture
def flows(monkeypatch):
    store = FlowStore()
    monkeypatch.setattr(oauth_router, "FLOWS", store)
    return store


@pytest.fixture
def token_endpoint(monkeypatch):
    endpoint = TokenEndpoint()

    def fake_post(url, data, timeout=None, **kwargs):
        assert url == oauth_wordpress.TOKEN_ENDPOINT
        endpoint.calls.append(dict(data))
        return endpoint.reply

    monkeypatch.setattr(oauth_wordpress.requests, "post", fake_post)
    return endpoint


@pytest.fixture
def client(env, flows, token_endpoint, caplog) -> TestClient:
    caplog.set_level(logging.DEBUG)
    app = FastAPI()
    install_security(app, allowed_origins=[UI])
    app.include_router(oauth_router.router, prefix="/api")
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def wp(env):
    return connections.create("wordpress", {"label": "blog", "client_id": WP_CLIENT_ID, "client_secret": WP_CLIENT_SECRET})


def _app_logs(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore", "asyncio")))


def _post(client, path, body):
    return client.post(path, json=body, headers=WRITE)


def _start(client, connection_id=WP_ID, mode="paste_back"):
    resp = _post(client, "/api/oauth/wordpress/start", {"connection_id": connection_id, "mode": mode})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    body["state"] = parse_qs(urlsplit(body["auth_url"]).query)["state"][0]
    return body


def _paste_url(state, code=CODE, **extra):
    params = {"state": state, "code": code, **extra}
    return "http://127.0.0.1:8767/?" + "&".join(f"{k}={v}" for k, v in params.items())


def _callback(client, **params):
    return client.get("/api/oauth/callback/wordpress", params=params, follow_redirects=False)


def _everything(resp) -> str:
    headers = "\n".join(f"{k}: {v}" for k, v in resp.headers.multi_items())
    return f"{resp.status_code}\n{headers}\n{resp.text}"


def _assert_clean(resp, *extra: str) -> None:
    text = _everything(resp)
    for value in (*SECRETS, *extra):
        assert value not in text, "a secret leaked"


def _assert_error_redirect(resp, reason: str) -> None:
    assert resp.status_code == 302
    assert resp.headers["location"] == f"{UI}/connections?oauth=error&reason={reason}"
    assert reason in oauth_router._REASONS


def _deletes(resp, name: str) -> bool:
    return any(line.startswith(f"{name}=") and "Max-Age=0" in line for line in resp.headers.get_list("set-cookie"))


# ---- start ----


def test_start_wordpress_sets_a_bound_nonce_cookie_and_leaks_nothing(client, wp, flows):
    resp = _post(client, "/api/oauth/wordpress/start", {"connection_id": WP_ID, "mode": "paste_back"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"flow_id", "auth_url", "mode", "expires_at", "instructions"}
    assert body["mode"] == "paste_back" and body["expires_at"].endswith("Z")
    assert body["auth_url"].startswith("https://public-api.wordpress.com/oauth2/authorize")
    assert parse_qs(urlsplit(body["auth_url"]).query)["scope"] == ["global"]

    cookies = resp.headers.get_list("set-cookie")
    assert len(cookies) == 1
    cookie = cookies[0]
    name = f"ss_oauth_{body['flow_id']}"
    assert cookie.startswith(f"{name}=") and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert WP_CLIENT_SECRET not in _everything(resp)
    assert len(flows) == 1


def test_start_callback_mode_builds_the_configured_redirect(client, wp, flows):
    started = _start(client, mode="callback")

    assert parse_qs(urlsplit(started["auth_url"]).query)["redirect_uri"] == [f"{BASE_URL}/api/oauth/callback/wordpress"]
    assert started["mode"] == "callback"


def test_start_kind_mismatch_and_missing_client_secret(client, wp, flows):
    other = connections.create("gmail", {"label": "personal", "client_id": "x", "client_secret": "y"})
    resp = _post(client, "/api/oauth/wordpress/start", {"connection_id": other.id, "mode": "paste_back"})
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "provider_kind_mismatch"

    resp = _post(client, "/api/oauth/wordpress/start", {"connection_id": "wordpress_nobody", "mode": "paste_back"})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "connection_not_found"
    assert len(flows) == 0


# ---- paste ----


def test_paste_wordpress_connects_and_stores_only_the_access_token(client, wp, token_endpoint, temp_db, flows):
    started = _start(client)

    resp = _post(client, "/api/oauth/wordpress/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "connected"
    assert body["account"] is None
    assert body["connection"]["id"] == WP_ID
    assert body["connection"]["secrets_set"] == ["access_token", "client_secret"]
    _assert_clean(resp)
    assert len(token_endpoint.calls) == 1
    assert token_endpoint.calls[0]["code"] == CODE
    assert token_endpoint.calls[0]["grant_type"] == "authorization_code"
    assert connections.get_secret(WP_ID, "access_token") == WP_ACCESS
    assert _deletes(resp, f"ss_oauth_{started['flow_id']}")
    assert len(flows) == 0


def test_paste_consent_denied_and_exchange_failure_use_fixed_messages(client, wp, token_endpoint, flows):
    denied = _start(client)
    resp = _post(
        client,
        "/api/oauth/wordpress/paste",
        {"flow_id": denied["flow_id"], "url": f"http://127.0.0.1:8767/?state={denied['state']}&error=access_denied"},
    )
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "consent_denied"

    failing = _start(client)
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_request", "error_description": f"bad {CODE}"})
    resp = _post(client, "/api/oauth/wordpress/paste", {"flow_id": failing["flow_id"], "url": _paste_url(failing["state"])})
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "exchange_failed"
    _assert_clean(resp)


def test_paste_with_a_state_from_another_sign_in_is_a_400(client, wp, token_endpoint, flows):
    first = _start(client)
    second = _start(client)

    resp = _post(client, "/api/oauth/wordpress/paste", {"flow_id": first["flow_id"], "url": _paste_url(second["state"])})

    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "state_mismatch"
    assert token_endpoint.calls == []


def test_paste_needs_the_request_guard_header(client, wp, flows):
    started = _start(client)

    resp = client.post("/api/oauth/wordpress/paste", json={"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 403 and len(flows) == 1


def test_paste_without_client_secret_is_client_credentials_missing(client, wp, token_endpoint, flows, monkeypatch):
    started = _start(client)
    # The secret was removed mid-flow (e.g. a bad vault key); _client_credentials must refuse, not exchange.
    monkeypatch.setattr(oauth_wordpress.connections, "get_secret", lambda *a, **k: None)

    resp = _post(client, "/api/oauth/wordpress/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "client_credentials_missing"
    assert token_endpoint.calls == []


# ---- callback ----


def test_callback_wordpress_happy_path_redirects_to_the_fixed_target(client, wp, token_endpoint, temp_db, flows, caplog):
    started = _start(client, mode="callback")
    name = f"ss_oauth_{started['flow_id']}"

    resp = _callback(client, state=started["state"], code=CODE)

    assert resp.status_code == 302
    assert resp.headers["location"] == SUCCESS
    assert resp.headers["cache-control"] == "no-store"
    assert resp.text == ""
    assert _deletes(resp, name)
    assert len(token_endpoint.calls) == 1
    assert token_endpoint.calls[0]["redirect_uri"] == f"{BASE_URL}/api/oauth/callback/wordpress"
    assert connections.get_secret(WP_ID, "access_token") == WP_ACCESS
    _assert_clean(resp, started["state"])
    assert started["state"] not in _app_logs(caplog) and CODE not in _app_logs(caplog)
    assert len(flows) == 0


def test_callback_provider_error_is_forwarded_and_mapped_to_a_fixed_reason(client, wp, token_endpoint, flows):
    denied = _start(client, mode="callback")
    resp = _callback(client, state=denied["state"], error="access_denied", error_description="user said no")
    _assert_error_redirect(resp, "denied")
    assert token_endpoint.calls == []
    assert "user said no" not in _everything(resp)

    other = _start(client, mode="callback")
    resp = _callback(client, state=other["state"], error="server_error <script>")
    _assert_error_redirect(resp, "provider_error")
    assert "script" not in _everything(resp)


def test_callback_without_a_code_ends_the_flow_as_a_provider_error(client, wp, token_endpoint, flows):
    started = _start(client, mode="callback")

    resp = _callback(client, state=started["state"])

    _assert_error_redirect(resp, "provider_error")
    assert token_endpoint.calls == [] and len(flows) == 0


def test_callback_exchange_failure_is_a_fixed_reason_and_leaks_nothing(client, wp, token_endpoint, flows, caplog):
    started = _start(client, mode="callback")
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_request", "error_description": f"bad {CODE}"})

    resp = _callback(client, state=started["state"], code=CODE)

    _assert_error_redirect(resp, "exchange_failed")
    assert _deletes(resp, f"ss_oauth_{started['flow_id']}")
    _assert_clean(resp, started["state"])
    assert CODE not in _app_logs(caplog) and started["state"] not in _app_logs(caplog)


def test_callback_with_a_forged_state_is_a_generic_redirect_with_no_side_effects(client, wp, token_endpoint, temp_db, flows):
    started = _start(client, mode="callback")

    resp = _callback(client, state="forged-state-Lp30", code=CODE)

    _assert_error_redirect(resp, "invalid_request")
    assert token_endpoint.calls == []
    assert len(flows) == 1
    assert "set-cookie" not in resp.headers
    assert _callback(client, state=started["state"], code=CODE).headers["location"] == SUCCESS


def test_callback_unexpected_exception_is_a_generic_failure_and_only_the_type_is_logged(client, wp, flows, monkeypatch, caplog):
    started = _start(client, mode="callback")

    def boom(*args, **kwargs):
        raise RuntimeError(f"secret detail {CODE}")

    monkeypatch.setattr(oauth_wordpress.requests, "post", boom)

    resp = _callback(client, state=started["state"], code=CODE)

    _assert_error_redirect(resp, "failed")
    assert CODE not in _everything(resp) and CODE not in _app_logs(caplog) and "secret detail" not in _app_logs(caplog)
