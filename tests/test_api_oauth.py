"""
Tests for api.routers.oauth. The true externals are stubbed: Google's token endpoint (requests.post inside
pipeline.oauth_google) and MSAL's network calls (PublicClientApplication). The FlowStore, the provider
modules, the connection store, the vault, the request guard and the SQLite file are all real.

Router-only app with install_security, never entered as a context manager so the real lifespan does not
run. What matters above all: the callback can only ever answer a 302 to a FIXED address, and no code,
state, verifier, nonce or token appears in a response, a header, a log line or the database.
"""
import base64
import json
import logging
import re
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import oauth as oauth_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import connections, crypto, db, health, oauth_flows, oauth_google, oauth_m365, oauth_zoom  # noqa: E402
from pipeline.health import GMAIL_SCOPE  # noqa: E402
from pipeline.oauth_flows import FlowStore  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}
UI = "https://ui.example.com"
BASE_URL = "https://hook.example.com"
SUCCESS = f"{UI}/connections?oauth=ok"
LOCATION_SHAPE = re.compile(r"^https://ui\.example\.com/connections\?oauth=(ok|error&reason=([a-z_]+))$")

G_CLIENT_ID = "1234-example.apps.googleusercontent.com"
G_CLIENT_SECRET = "gmail-client-secret-Tj40cUe5Ya"
G_REFRESH = "gmail-refresh-Vn27sGf1Lo"
G_ACCESS = "gmail-access-Qm18wEs7Uc"
G_ID = "gmail_personal"

TENANT_ID = "00000000-1111-2222-3333-444444444444"
M_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
M_ID = "m365_acme"
M_ACCOUNT = "alex@example.com"
M_ACCESS = "ms-access-token-Hd91XvB7mR"
M_REFRESH = "ms-refresh-token-Bk55Lq0Wz"
VERIFIER = "pkce-verifier-Rt61xHd0Pa"

Z_ID = "zoom"
Z_CLIENT_ID = "zoom-client-id-Ab12Cd34Ef"
Z_CLIENT_SECRET = "zoom-client-secret-Gh56Ij78Kl"
Z_REFRESH = "zoom-refresh-Mn90Op12Qr"
Z_ACCESS = "zoom-access-St34Uv56Wx"

CODE = "4/auth-code-Zk29qLmXw"
SECRETS = [G_CLIENT_SECRET, G_REFRESH, G_ACCESS, M_ACCESS, M_REFRESH, VERIFIER, CODE, Z_CLIENT_SECRET, Z_REFRESH, Z_ACCESS]


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class TokenEndpoint:
    def __init__(self):
        self.calls: list[dict] = []
        self.reply = _FakeResponse(200, {"refresh_token": G_REFRESH, "scope": GMAIL_SCOPE, "access_token": G_ACCESS})


class FakeMsal:
    """Stands in for msal.PublicClientApplication; only the two flow calls and get_accounts are needed."""

    def __init__(self):
        self.acquire_calls: list[dict] = []
        self.reply: dict = {
            "access_token": M_ACCESS,
            "refresh_token": M_REFRESH,
            "id_token_claims": {"preferred_username": M_ACCOUNT},
        }
        self._count = 0

    def app_class(self):
        fake = self

        class FakeApp:
            def __init__(self, client_id, authority=None, token_cache=None, **kwargs):
                assert token_cache is not None
                self.cache = token_cache

            def initiate_auth_code_flow(self, scopes, redirect_uri=None, **kwargs):
                fake._count += 1
                state = f"ms-state-{fake._count}-Wq82nKd"
                return {
                    "state": state,
                    "redirect_uri": redirect_uri,
                    "scope": list(scopes),
                    "code_verifier": VERIFIER,
                    "auth_uri": (
                        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize"
                        f"?client_id={M_CLIENT_ID}&state={state}&code_challenge=c&response_type=code"
                    ),
                }

            def acquire_token_by_auth_code_flow(self, auth_code_flow, auth_response, **kwargs):
                fake.acquire_calls.append(dict(auth_response))
                if auth_response.get("state") != auth_code_flow["state"]:
                    raise ValueError("State mismatch")
                if "error" in fake.reply:
                    return dict(fake.reply)
                self.cache.add(
                    {
                        "client_id": M_CLIENT_ID,
                        "scope": ["Mail.Read"],
                        "token_endpoint": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
                        "response": {
                            "access_token": fake.reply["access_token"],
                            "refresh_token": fake.reply["refresh_token"],
                            "expires_in": 3600,
                            "token_type": "Bearer",
                            "id_token_claims": fake.reply.get("id_token_claims", {}),
                            "client_info": base64.urlsafe_b64encode(
                                json.dumps({"uid": "u1", "utid": TENANT_ID}).encode()
                            ).decode(),
                        },
                        "data": {},
                    }
                )
                return dict(fake.reply)

            def get_accounts(self):
                return []

        return FakeApp


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def env(monkeypatch, tmp_path, temp_db):
    """A vault, an empty .env, the token dir in tmp, the configured UI origin and a public https base URL."""
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(health, "TOKEN_DIR", tmp_path / "tokens")
    monkeypatch.setenv("WEB_ORIGINS", UI)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    health.set_env_overlay_provider(connections.overlay_provider, connections.is_family_key)
    yield tmp_path
    connections.set_vault(None)


@pytest.fixture
def flows(monkeypatch):
    """A fresh real FlowStore installed as the router's singleton, so tests do not share flows."""
    store = FlowStore()
    monkeypatch.setattr(oauth_router, "FLOWS", store)
    return store


@pytest.fixture
def token_endpoint(monkeypatch):
    endpoint = TokenEndpoint()

    def fake_post(url, data, timeout=None, **kwargs):
        assert url == oauth_google.TOKEN_ENDPOINT
        endpoint.calls.append(dict(data))
        return endpoint.reply

    monkeypatch.setattr(oauth_google.requests, "post", fake_post)
    return endpoint


@pytest.fixture
def zoom_token_endpoint(monkeypatch):
    endpoint = TokenEndpoint()
    endpoint.reply = _FakeResponse(200, {"refresh_token": Z_REFRESH, "access_token": Z_ACCESS, "expires_in": 3600})

    def fake_post(url, data, timeout=None, auth=None, **kwargs):
        assert url == oauth_zoom.TOKEN_ENDPOINT
        endpoint.calls.append(dict(data))
        return endpoint.reply

    monkeypatch.setattr(oauth_zoom.requests, "post", fake_post)
    return endpoint


@pytest.fixture
def fake_msal(monkeypatch):
    fake = FakeMsal()
    monkeypatch.setattr(oauth_m365.msal, "PublicClientApplication", fake.app_class())
    return fake


@pytest.fixture
def client(env, flows, token_endpoint, fake_msal, caplog) -> TestClient:
    caplog.set_level(logging.DEBUG)
    app = FastAPI()
    install_security(app, allowed_origins=[UI])
    app.include_router(oauth_router.router, prefix="/api")
    # No default X-Requested-With: the callback is a GET and must not need it. POST helpers add it.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def gmail(env):
    return connections.create("gmail", {"label": "personal", "client_id": G_CLIENT_ID, "client_secret": G_CLIENT_SECRET})


@pytest.fixture
def m365(env):
    return connections.create("m365", {"alias": "acme", "tenant_id": TENANT_ID, "client_id": M_CLIENT_ID})


@pytest.fixture
def zoom(env):
    return connections.create("zoom", {"client_id": Z_CLIENT_ID, "client_secret": Z_CLIENT_SECRET})


def _app_logs(caplog) -> str:
    """Everything the app logged. The test client's own httpx request log (it prints the URL) is not the app."""
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore", "asyncio")))


def _post(client, path, body):
    return client.post(path, json=body, headers=WRITE)


def _start(client, provider, connection_id, mode="paste_back"):
    resp = _post(client, f"/api/oauth/{provider}/start", {"connection_id": connection_id, "mode": mode})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    body["state"] = parse_qs(urlsplit(body["auth_url"]).query)["state"][0]
    return body


def _paste_url(state, code=CODE):
    return f"http://127.0.0.1:8765/?state={state}&code={code}&scope=x"


def _callback(client, provider, **params):
    return client.get(f"/api/oauth/callback/{provider}", params=params, follow_redirects=False)


def _raw_db_text(engine) -> str:
    parts = []
    with engine.connect() as conn:
        for table in ("connection", "tombstone"):
            for row in conn.exec_driver_sql(f"SELECT * FROM {table}"):
                parts.extend(str(col) for col in row)
    return "\n".join(parts)


def _everything(resp) -> str:
    """The whole response as text: status line, headers (Location, Set-Cookie) and body."""
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
    return any(
        line.startswith(f"{name}=") and "Max-Age=0" in line for line in resp.headers.get_list("set-cookie")
    )


# ---- start ----


def test_start_google_sets_a_bound_nonce_cookie_and_leaks_nothing(client, gmail, flows):
    resp = _post(client, "/api/oauth/google/start", {"connection_id": G_ID, "mode": "paste_back"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"flow_id", "auth_url", "mode", "expires_at", "instructions"}
    assert body["mode"] == "paste_back" and body["expires_at"].endswith("Z")
    assert body["auth_url"].startswith("https://accounts.google.com/")

    cookies = resp.headers.get_list("set-cookie")
    assert len(cookies) == 1
    cookie = cookies[0]
    name = f"ss_oauth_{body['flow_id']}"
    assert cookie.startswith(f"{name}=")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/oauth" in cookie
    assert "Secure" in cookie  # PUBLIC_BASE_URL is https
    max_age = re.search(r"Max-Age=(\d+)", cookie)
    assert max_age is not None and 0 < int(max_age.group(1)) <= 900

    nonce = cookie.split("=", 1)[1].split(";", 1)[0]
    stored = next(iter(flows._flows.values()))
    verifier = stored.payload["code_verifier"]
    assert nonce and nonce not in resp.text
    assert verifier not in _everything(resp)
    assert G_CLIENT_SECRET not in _everything(resp)
    assert len(flows) == 1


def test_start_microsoft_sets_the_cookie_and_hides_the_flow_dict(client, m365, flows):
    resp = _post(client, "/api/oauth/microsoft/start", {"connection_id": M_ID, "mode": "paste_back"})

    assert resp.status_code == 200
    cookie = resp.headers.get_list("set-cookie")[0]
    assert cookie.startswith(f"ss_oauth_{resp.json()['flow_id']}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/api/oauth" in cookie
    assert VERIFIER not in _everything(resp)


def test_start_cookie_is_not_secure_without_an_https_public_base_url(client, gmail, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL")

    resp = _post(client, "/api/oauth/google/start", {"connection_id": G_ID, "mode": "paste_back"})

    assert resp.status_code == 200
    cookie = resp.headers.get_list("set-cookie")[0]
    assert "Secure" not in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_start_needs_the_request_guard_header(client, gmail, flows):
    resp = client.post("/api/oauth/google/start", json={"connection_id": G_ID, "mode": "paste_back"})

    assert resp.status_code == 403
    assert len(flows) == 0


def test_start_callback_mode_is_refused_without_a_public_base_url(client, gmail, m365, flows, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL")

    for provider, connection_id in (("google", G_ID), ("microsoft", M_ID)):
        resp = _post(client, f"/api/oauth/{provider}/start", {"connection_id": connection_id, "mode": "callback"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "callback_unavailable"
        assert "set-cookie" not in resp.headers
    assert len(flows) == 0


def test_start_callback_mode_ignores_a_non_https_public_base_url(client, gmail, flows, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://hook.example.com")

    resp = _post(client, "/api/oauth/google/start", {"connection_id": G_ID, "mode": "callback"})

    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "callback_unavailable"


def test_start_callback_mode_builds_the_configured_redirect(client, gmail, m365, flows):
    g = _start(client, "google", G_ID, "callback")
    m = _start(client, "microsoft", M_ID, "callback")

    assert parse_qs(urlsplit(g["auth_url"]).query)["redirect_uri"] == [f"{BASE_URL}/api/oauth/callback/google"]
    assert m["mode"] == "callback"
    assert len(flows) == 2


@pytest.mark.parametrize(
    "provider, connection_id",
    [("microsoft", G_ID), ("google", M_ID)],
)
def test_start_kind_and_provider_mismatch_is_a_400(client, gmail, m365, flows, provider, connection_id):
    resp = _post(client, f"/api/oauth/{provider}/start", {"connection_id": connection_id, "mode": "paste_back"})

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "provider_kind_mismatch"
    assert "set-cookie" not in resp.headers
    assert len(flows) == 0


def test_start_unknown_provider_connection_and_mode(client, gmail, flows):
    assert _post(client, "/api/oauth/github/start", {"connection_id": G_ID, "mode": "paste_back"}).status_code == 404
    missing = _post(client, "/api/oauth/google/start", {"connection_id": "gmail_nobody", "mode": "paste_back"})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "connection_not_found"
    bad_mode = _post(client, "/api/oauth/google/start", {"connection_id": G_ID, "mode": "carrier-pigeon"})
    assert bad_mode.status_code == 422
    assert "carrier-pigeon" not in bad_mode.text
    assert len(flows) == 0


# ---- paste ----


def test_paste_google_connects_and_stores_only_the_refresh_token(client, gmail, token_endpoint, temp_db, flows):
    started = _start(client, "google", G_ID)

    resp = _post(client, "/api/oauth/google/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "connected"
    assert body["account"] is None
    assert body["connection"]["id"] == G_ID
    assert "refresh_token" in body["connection"]["secrets_set"]
    _assert_clean(resp)
    assert len(token_endpoint.calls) == 1
    assert token_endpoint.calls[0]["code"] == CODE
    assert connections.get_secret(G_ID, "refresh_token") == G_REFRESH
    raw = _raw_db_text(temp_db)
    assert G_REFRESH not in raw and CODE not in raw and G_CLIENT_SECRET not in raw
    assert _deletes(resp, f"ss_oauth_{started['flow_id']}")
    assert len(flows) == 0


def test_paste_microsoft_connects_and_returns_the_account(client, m365, fake_msal, env, flows):
    started = _start(client, "microsoft", M_ID)

    resp = _post(
        client,
        "/api/oauth/microsoft/paste",
        {"flow_id": started["flow_id"], "url": f"http://localhost/?code={CODE}&state={started['state']}"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "connected" and body["account"] == M_ACCOUNT
    assert body["connection"]["id"] == M_ID
    _assert_clean(resp)
    assert (env / "tokens" / "acme_cache.bin").exists()
    assert fake_msal.acquire_calls == [{"code": CODE, "state": started["state"]}]


def test_paste_without_the_cookie_fails_and_does_not_consume(client, gmail, token_endpoint, flows):
    started = _start(client, "google", G_ID)
    cookie_name = f"ss_oauth_{started['flow_id']}"
    nonce = client.cookies[cookie_name]
    client.cookies.clear()

    resp = _post(client, "/api/oauth/google/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "nonce_mismatch"
    assert token_endpoint.calls == []
    assert len(flows) == 1
    _assert_clean(resp)

    client.cookies.set(cookie_name, nonce, path="/api/oauth")
    retry = _post(client, "/api/oauth/google/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])})
    assert retry.status_code == 200
    assert len(flows) == 0


def test_paste_with_a_wrong_cookie_fails_and_does_not_consume(client, gmail, token_endpoint, flows):
    started = _start(client, "google", G_ID)
    cookie_name = f"ss_oauth_{started['flow_id']}"
    good = client.cookies[cookie_name]
    client.cookies.clear()
    client.cookies.set(cookie_name, "not-the-nonce", path="/api/oauth")

    resp = _post(client, "/api/oauth/google/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 403
    assert token_endpoint.calls == [] and len(flows) == 1

    client.cookies.clear()
    client.cookies.set(cookie_name, good, path="/api/oauth")
    assert _post(client, "/api/oauth/google/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])}).status_code == 200


def test_paste_with_another_flows_cookie_is_not_accepted(client, gmail, token_endpoint, flows):
    first = _start(client, "google", G_ID)
    second = _start(client, "google", G_ID)
    other_nonce = client.cookies[f"ss_oauth_{second['flow_id']}"]
    client.cookies.clear()
    client.cookies.set(f"ss_oauth_{first['flow_id']}", other_nonce, path="/api/oauth")

    resp = _post(client, "/api/oauth/google/paste", {"flow_id": first["flow_id"], "url": _paste_url(first["state"])})

    assert resp.status_code == 403
    assert len(flows) == 2 and token_endpoint.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/?code=x&state=y",
        "http://127.0.0.1@evil.example/?code=x&state=y",
        "http://127.0.0.1:8765/#code=x&state=y",
        "not a url",
    ],
)
def test_paste_of_an_unusable_url_is_a_400_and_keeps_the_flow(client, gmail, token_endpoint, flows, url):
    started = _start(client, "google", G_ID)

    resp = _post(client, "/api/oauth/google/paste", {"flow_id": started["flow_id"], "url": url})

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_pasted_url"
    assert url not in resp.text
    assert token_endpoint.calls == [] and len(flows) == 1


def test_paste_with_a_state_from_another_sign_in_is_a_400(client, gmail, token_endpoint, flows):
    first = _start(client, "google", G_ID)
    second = _start(client, "google", G_ID)

    resp = _post(client, "/api/oauth/google/paste", {"flow_id": first["flow_id"], "url": _paste_url(second["state"])})

    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "state_mismatch"
    assert token_endpoint.calls == []
    _assert_clean(resp)


def test_paste_consent_denied_and_exchange_failure_use_fixed_messages(client, gmail, token_endpoint, flows):
    denied = _start(client, "google", G_ID)
    resp = _post(
        client,
        "/api/oauth/google/paste",
        {"flow_id": denied["flow_id"], "url": f"http://127.0.0.1:8765/?state={denied['state']}&error=access_denied"},
    )
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "consent_denied"

    failing = _start(client, "google", G_ID)
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_grant", "error_description": f"bad {CODE} {VERIFIER}"})
    resp = _post(client, "/api/oauth/google/paste", {"flow_id": failing["flow_id"], "url": _paste_url(failing["state"])})
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "exchange_failed"
    _assert_clean(resp)


def test_paste_on_the_wrong_provider_path_is_refused_and_keeps_the_flow(client, gmail, m365, token_endpoint, flows):
    started = _start(client, "google", G_ID)

    resp = _post(client, "/api/oauth/microsoft/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "provider_mismatch"
    assert token_endpoint.calls == [] and len(flows) == 1


def test_paste_unknown_flow_and_provider(client, gmail, flows):
    resp = _post(client, "/api/oauth/google/paste", {"flow_id": "nope", "url": _paste_url("s")})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "unknown_flow"
    assert _post(client, "/api/oauth/github/paste", {"flow_id": "nope", "url": _paste_url("s")}).status_code == 404


def test_paste_needs_the_request_guard_header(client, gmail, flows):
    started = _start(client, "google", G_ID)

    resp = client.post("/api/oauth/google/paste", json={"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 403 and len(flows) == 1


def test_paste_expired_flow_is_a_410(client, gmail, token_endpoint, flows, monkeypatch):
    started = _start(client, "google", G_ID)
    later = oauth_flows.utcnow() + timedelta(seconds=oauth_flows.DEFAULT_TTL_SECONDS + 5)
    monkeypatch.setattr(oauth_flows, "utcnow", lambda: later)

    resp = _post(client, "/api/oauth/google/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 410 and resp.json()["detail"]["code"] == "flow_expired"
    assert token_endpoint.calls == []


# ---- callback ----


def test_callback_google_happy_path_redirects_to_the_fixed_target(client, gmail, token_endpoint, temp_db, flows, caplog):
    started = _start(client, "google", G_ID, "callback")
    name = f"ss_oauth_{started['flow_id']}"

    resp = _callback(client, "google", state=started["state"], code=CODE)

    assert resp.status_code == 302
    assert resp.headers["location"] == SUCCESS
    assert resp.headers["cache-control"] == "no-store"
    assert resp.text == ""
    assert _deletes(resp, name)
    assert len(token_endpoint.calls) == 1
    assert token_endpoint.calls[0]["redirect_uri"] == f"{BASE_URL}/api/oauth/callback/google"
    assert connections.get_secret(G_ID, "refresh_token") == G_REFRESH
    _assert_clean(resp, started["state"])
    assert started["state"] not in _app_logs(caplog) and CODE not in _app_logs(caplog)
    raw = _raw_db_text(temp_db)
    assert CODE not in raw and G_REFRESH not in raw
    assert len(flows) == 0


def test_callback_microsoft_happy_path(client, m365, fake_msal, env, flows, caplog):
    started = _start(client, "microsoft", M_ID, "callback")

    resp = _callback(client, "microsoft", state=started["state"], code=CODE, session_state="abc")

    assert resp.headers["location"] == SUCCESS and resp.status_code == 302
    assert _deletes(resp, f"ss_oauth_{started['flow_id']}")
    assert (env / "tokens" / "acme_cache.bin").exists()
    assert fake_msal.acquire_calls == [{"code": CODE, "state": started["state"]}]
    _assert_clean(resp, started["state"])
    assert started["state"] not in _app_logs(caplog) and CODE not in _app_logs(caplog)


def test_callback_needs_no_request_guard_header(client, gmail, token_endpoint, flows):
    started = _start(client, "google", G_ID, "callback")
    assert "x-requested-with" not in {k.lower() for k in client.headers}

    resp = _callback(client, "google", state=started["state"], code=CODE)

    assert resp.status_code == 302 and resp.headers["location"] == SUCCESS


def test_callback_with_a_forged_state_is_a_generic_redirect_with_no_side_effects(
    client, gmail, token_endpoint, temp_db, flows, caplog
):
    started = _start(client, "google", G_ID, "callback")
    before = _raw_db_text(temp_db)

    resp = _callback(client, "google", state="forged-state-Lp30", code=CODE)

    _assert_error_redirect(resp, "invalid_request")
    assert token_endpoint.calls == []
    assert _raw_db_text(temp_db) == before
    assert len(flows) == 1
    assert "set-cookie" not in resp.headers  # the real flow's cookie is not touched
    assert "forged-state-Lp30" not in _everything(resp) and CODE not in _everything(resp)
    assert "forged-state-Lp30" not in _app_logs(caplog) and CODE not in _app_logs(caplog)
    # the legitimate sign-in still completes
    assert _callback(client, "google", state=started["state"], code=CODE).headers["location"] == SUCCESS


def test_callback_without_the_nonce_cookie_fails_and_keeps_the_flow(client, gmail, token_endpoint, flows):
    started = _start(client, "google", G_ID, "callback")
    name = f"ss_oauth_{started['flow_id']}"
    nonce = client.cookies[name]
    client.cookies.clear()

    resp = _callback(client, "google", state=started["state"], code=CODE)

    _assert_error_redirect(resp, "invalid_request")
    assert token_endpoint.calls == [] and len(flows) == 1

    client.cookies.set(name, nonce, path="/api/oauth")
    assert _callback(client, "google", state=started["state"], code=CODE).headers["location"] == SUCCESS


def test_callback_with_a_stale_cookie_next_to_the_real_one_deletes_only_the_real_one(
    client, gmail, token_endpoint, flows
):
    client.cookies.set("ss_oauth_staleflow", "stale-nonce", path="/api/oauth")
    started = _start(client, "google", G_ID, "callback")

    resp = _callback(client, "google", state=started["state"], code=CODE)

    assert resp.headers["location"] == SUCCESS
    assert _deletes(resp, f"ss_oauth_{started['flow_id']}")
    assert not _deletes(resp, "ss_oauth_staleflow")


def test_callback_replay_is_refused(client, gmail, token_endpoint, flows):
    started = _start(client, "google", G_ID, "callback")
    assert _callback(client, "google", state=started["state"], code=CODE).headers["location"] == SUCCESS
    client.cookies.set(f"ss_oauth_{started['flow_id']}", "whatever", path="/api/oauth")

    replay = _callback(client, "google", state=started["state"], code=CODE)

    _assert_error_redirect(replay, "invalid_request")
    assert len(token_endpoint.calls) == 1


def test_callback_provider_mismatch_is_generic_and_keeps_the_flow(client, gmail, m365, token_endpoint, fake_msal, flows):
    started = _start(client, "google", G_ID, "callback")

    resp = _callback(client, "microsoft", state=started["state"], code=CODE)

    _assert_error_redirect(resp, "invalid_request")
    assert token_endpoint.calls == [] and fake_msal.acquire_calls == []
    assert len(flows) == 1
    assert "set-cookie" not in resp.headers
    assert _callback(client, "google", state=started["state"], code=CODE).headers["location"] == SUCCESS


def test_callback_unknown_provider_path_gets_the_generic_redirect(client, gmail, token_endpoint, flows):
    started = _start(client, "google", G_ID, "callback")

    resp = _callback(client, "github", state=started["state"], code=CODE)

    _assert_error_redirect(resp, "invalid_request")
    assert token_endpoint.calls == [] and len(flows) == 1


def test_callback_provider_error_is_forwarded_and_mapped_to_a_fixed_reason(client, gmail, token_endpoint, flows):
    denied = _start(client, "google", G_ID, "callback")
    resp = _callback(client, "google", state=denied["state"], error="access_denied", error_description="user said no")
    _assert_error_redirect(resp, "denied")
    assert token_endpoint.calls == []
    assert _deletes(resp, f"ss_oauth_{denied['flow_id']}")
    assert "user said no" not in _everything(resp)

    other = _start(client, "google", G_ID, "callback")
    resp = _callback(client, "google", state=other["state"], error="server_error <script>")
    _assert_error_redirect(resp, "provider_error")
    assert "script" not in _everything(resp)


def test_callback_microsoft_provider_error_text_is_never_surfaced(client, m365, fake_msal, flows):
    started = _start(client, "microsoft", M_ID, "callback")

    resp = _callback(client, "microsoft", state=started["state"], error="access_denied", error_description="AADSTS65004 nope")

    _assert_error_redirect(resp, "denied")
    assert "AADSTS" not in _everything(resp) and fake_msal.acquire_calls == []


def test_callback_exchange_failure_is_a_fixed_reason_and_leaks_nothing(client, gmail, token_endpoint, flows, caplog):
    started = _start(client, "google", G_ID, "callback")
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_grant", "error_description": f"bad {CODE} {VERIFIER}"})

    resp = _callback(client, "google", state=started["state"], code=CODE)

    _assert_error_redirect(resp, "exchange_failed")
    assert _deletes(resp, f"ss_oauth_{started['flow_id']}")
    _assert_clean(resp, started["state"])
    assert CODE not in _app_logs(caplog) and VERIFIER not in _app_logs(caplog) and started["state"] not in _app_logs(caplog)


def test_callback_missing_scope_and_missing_refresh_token(client, gmail, token_endpoint, flows):
    token_endpoint.reply = _FakeResponse(200, {"refresh_token": G_REFRESH, "scope": "openid", "access_token": G_ACCESS})
    scope = _start(client, "google", G_ID, "callback")
    _assert_error_redirect(_callback(client, "google", state=scope["state"], code=CODE), "scope_missing")

    token_endpoint.reply = _FakeResponse(200, {"scope": GMAIL_SCOPE, "access_token": G_ACCESS})
    norefresh = _start(client, "google", G_ID, "callback")
    _assert_error_redirect(_callback(client, "google", state=norefresh["state"], code=CODE), "no_refresh_token")


def test_callback_without_a_code_ends_the_flow_as_a_provider_error(client, gmail, token_endpoint, flows):
    started = _start(client, "google", G_ID, "callback")

    resp = _callback(client, "google", state=started["state"])

    _assert_error_redirect(resp, "provider_error")
    assert token_endpoint.calls == [] and len(flows) == 0


def test_callback_expired_flow_is_an_expired_reason(client, gmail, token_endpoint, flows, monkeypatch):
    started = _start(client, "google", G_ID, "callback")
    later = oauth_flows.utcnow() + timedelta(seconds=oauth_flows.DEFAULT_TTL_SECONDS + 5)
    monkeypatch.setattr(oauth_flows, "utcnow", lambda: later)

    resp = _callback(client, "google", state=started["state"], code=CODE)

    _assert_error_redirect(resp, "expired")
    assert token_endpoint.calls == []


def test_callback_connection_deleted_mid_flow_is_a_generic_failure(client, gmail, token_endpoint, flows):
    started = _start(client, "google", G_ID, "callback")
    connections.delete(G_ID)

    resp = _callback(client, "google", state=started["state"], code=CODE)

    _assert_error_redirect(resp, "failed")
    assert token_endpoint.calls == []


def test_callback_unexpected_exception_is_a_generic_failure_and_only_the_type_is_logged(
    client, gmail, flows, monkeypatch, caplog
):
    started = _start(client, "google", G_ID, "callback")

    def boom(*args, **kwargs):
        raise RuntimeError(f"secret detail {CODE}")

    monkeypatch.setattr(oauth_google.requests, "post", boom)

    resp = _callback(client, "google", state=started["state"], code=CODE)

    _assert_error_redirect(resp, "failed")
    assert CODE not in _everything(resp) and CODE not in _app_logs(caplog) and "secret detail" not in _app_logs(caplog)


ATTACKER_VALUES = [
    "https://evil.example/steal",
    "//evil.example/x",
    "\\\\evil.example",
    "http://evil.example@ui.example.com/",
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "/connections?oauth=ok",
    "ok&reason=x\r\nSet-Cookie: pwn=1",
    "%0d%0aLocation: https://evil.example",
    "a" * 5000,
    "‮\u0000﻿",
    "",
]


@pytest.mark.parametrize("value", ATTACKER_VALUES)
def test_fuzzed_query_parameters_never_reach_the_redirect(client, gmail, m365, token_endpoint, fake_msal, flows, value):
    started = _start(client, "google", G_ID, "callback")
    bodies = [
        {"state": value, "code": value, "error": value},
        {"state": value, "code": CODE},
        {"state": started["state"], "code": value},
        {"state": started["state"], "error": value},
    ]
    for params in bodies:
        for provider in ("google", "microsoft", "github"):
            resp = _callback(client, provider, **params)
            location = resp.headers["location"]
            assert resp.status_code == 302
            match = LOCATION_SHAPE.match(location)
            assert match, location
            assert match.group(2) is None or match.group(2) in oauth_router._REASONS
            assert "evil" not in location.lower()
            assert urlsplit(location).netloc == "ui.example.com"
            if len(value) > 8 and value != "/connections?oauth=ok":
                assert value not in _everything(resp)
            if not resp.headers["location"] == SUCCESS:
                assert resp.text == ""
        # a successful earlier iteration can consume the flow; start a new one for the next
        if not flows._flows:
            started = _start(client, "google", G_ID, "callback")


def test_fuzz_path_provider_values_cannot_redirect_elsewhere(client, gmail, flows):
    for provider in ("%2e%2e", "google%2F..%2F", "evil.example", "GOOGLE", "microsoft%00"):
        resp = client.get(f"/api/oauth/callback/{provider}?state=a&code=b", follow_redirects=False)
        if resp.status_code == 302:
            assert LOCATION_SHAPE.match(resp.headers["location"])
        else:
            assert resp.status_code in (404, 307, 308)


def test_no_flow_or_redirect_string_carries_the_code(client, gmail, token_endpoint, flows, caplog):
    started = _start(client, "google", G_ID, "callback")
    responses = [
        _callback(client, "google", state="forged", code=CODE),
        _callback(client, "google", state=started["state"], code=CODE),
        _callback(client, "google", state=started["state"], code=CODE),
        _callback(client, "microsoft", state=started["state"], code=CODE),
    ]

    for resp in responses:
        assert CODE not in _everything(resp)
    assert CODE not in _app_logs(caplog)
    assert started["state"] not in _app_logs(caplog)


def test_callback_without_a_usable_ui_origin_answers_plain_text_not_a_redirect(client, gmail, token_endpoint, flows, monkeypatch):
    monkeypatch.setenv("WEB_ORIGINS", "*, null")
    started = _start(client, "google", G_ID, "callback")

    ok = _callback(client, "google", state=started["state"], code=CODE)
    bad = _callback(client, "google", state="forged", code=CODE)

    assert ok.status_code == 200 and "location" not in ok.headers and _deletes(ok, f"ss_oauth_{started['flow_id']}")
    assert bad.status_code == 400 and "location" not in bad.headers
    assert CODE not in ok.text + bad.text


def test_callback_uses_the_first_usable_configured_origin(client, gmail, token_endpoint, flows, monkeypatch):
    monkeypatch.setenv("WEB_ORIGINS", "*, HTTPS://Ui.Example.com/ , https://other.example.org")
    started = _start(client, "google", G_ID, "callback")

    resp = _callback(client, "google", state=started["state"], code=CODE)

    assert resp.headers["location"] == SUCCESS


# ---- zoom ----


def test_start_zoom_sets_a_bound_nonce_cookie_and_builds_the_configured_redirect(client, zoom, flows):
    started = _start(client, "zoom", Z_ID, "callback")

    assert started["mode"] == "callback"
    assert parse_qs(urlsplit(started["auth_url"]).query)["redirect_uri"] == [f"{BASE_URL}/api/oauth/callback/zoom"]
    cookie = client.cookies[f"ss_oauth_{started['flow_id']}"]
    assert cookie
    assert Z_CLIENT_SECRET not in started["auth_url"]
    assert len(flows) == 1


def test_start_zoom_paste_back_mode_is_refused_with_invalid_mode(client, zoom, flows):
    resp = _post(client, "/api/oauth/zoom/start", {"connection_id": Z_ID, "mode": "paste_back"})

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_mode"
    assert "set-cookie" not in resp.headers
    assert len(flows) == 0


def test_start_zoom_with_s2s_auth_mode_is_a_handled_4xx_not_a_500(client, env, flows):
    connections.create("zoom", {"client_id": Z_CLIENT_ID, "client_secret": Z_CLIENT_SECRET, "auth_mode": "s2s", "account_id": "acct-1"})

    resp = _post(client, "/api/oauth/zoom/start", {"connection_id": Z_ID, "mode": "callback"})

    assert 400 <= resp.status_code < 500
    assert resp.json()["detail"]["code"] == "connection_not_found"
    assert len(flows) == 0


def test_paste_zoom_is_always_refused_and_never_touches_the_flow_store(client, zoom, flows):
    started = _start(client, "zoom", Z_ID, "callback")

    resp = _post(client, "/api/oauth/zoom/paste", {"flow_id": started["flow_id"], "url": _paste_url(started["state"])})

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_mode"
    assert len(flows) == 1  # the callback flow started above is untouched
    assert "set-cookie" not in resp.headers


def test_callback_zoom_happy_path_redirects_to_the_fixed_target(client, zoom, zoom_token_endpoint, temp_db, flows, caplog):
    started = _start(client, "zoom", Z_ID, "callback")
    name = f"ss_oauth_{started['flow_id']}"

    resp = _callback(client, "zoom", state=started["state"], code=CODE)

    assert resp.status_code == 302
    assert resp.headers["location"] == SUCCESS
    assert resp.headers["cache-control"] == "no-store"
    assert _deletes(resp, name)
    assert len(zoom_token_endpoint.calls) == 1
    assert zoom_token_endpoint.calls[0]["code"] == CODE
    assert zoom_token_endpoint.calls[0]["redirect_uri"] == f"{BASE_URL}/api/oauth/callback/zoom"
    assert connections.get_secret(Z_ID, "refresh_token") == Z_REFRESH
    _assert_clean(resp, started["state"])
    assert started["state"] not in _app_logs(caplog) and CODE not in _app_logs(caplog)
    raw = _raw_db_text(temp_db)
    assert CODE not in raw and Z_REFRESH not in raw
    assert len(flows) == 0


def test_callback_zoom_consent_denied_is_a_fixed_reason_and_leaks_nothing(client, zoom, zoom_token_endpoint, flows):
    started = _start(client, "zoom", Z_ID, "callback")

    resp = _callback(client, "zoom", state=started["state"], error="access_denied", error_description="user said no")

    _assert_error_redirect(resp, "denied")
    assert zoom_token_endpoint.calls == []
    assert _deletes(resp, f"ss_oauth_{started['flow_id']}")
    assert "user said no" not in _everything(resp)


def test_callback_zoom_exchange_failure_is_a_fixed_reason(client, zoom, zoom_token_endpoint, flows):
    started = _start(client, "zoom", Z_ID, "callback")
    zoom_token_endpoint.reply = _FakeResponse(400, {"error": "invalid_grant", "reason": f"bad {CODE}"})

    resp = _callback(client, "zoom", state=started["state"], code=CODE)

    _assert_error_redirect(resp, "exchange_failed")
    assert _deletes(resp, f"ss_oauth_{started['flow_id']}")
    _assert_clean(resp, started["state"])
