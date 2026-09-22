"""
Tests for GET /api/oauth/flows/{flow_id}: the browser's one authoritative answer to "did my sign-in
finish?". The true externals are stubbed (Google's token endpoint, MSAL's flow start); the FlowStore, the
provider modules, the connection store, the vault and the request guard are real.

Router-only app, never entered as a context manager so the real lifespan does not run. Cookies are sent
as explicit Cookie headers: the callback only tries the first MAX_PENDING cookies a browser sends, so a
shared cookie jar would make the many-flows test depend on jar order.
"""
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
from pipeline import connections, crypto, db, health, oauth_flows, oauth_google, oauth_m365  # noqa: E402
from pipeline.health import GMAIL_SCOPE  # noqa: E402
from pipeline.oauth_flows import FlowStore  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}
UI = "https://ui.example.com"
BASE_URL = "https://hook.example.com"
SUCCESS = f"{UI}/connections?oauth=ok"

G_CLIENT_ID = "1234-example.apps.googleusercontent.com"
G_CLIENT_SECRET = "gmail-client-secret-Tj40cUe5Ya"
G_REFRESH = "gmail-refresh-Vn27sGf1Lo"
G_ACCESS = "gmail-access-Qm18wEs7Uc"
G_ID = "gmail_personal"

TENANT_ID = "00000000-1111-2222-3333-444444444444"
M_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
M_ID = "m365_acme"
VERIFIER = "pkce-verifier-Rt61xHd0Pa"

CODE = "4/auth-code-Zk29qLmXw"
SECRETS = [G_CLIENT_SECRET, G_REFRESH, G_ACCESS, VERIFIER, CODE]


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
        self.raises: Exception | None = None


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
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
    store = FlowStore()
    monkeypatch.setattr(oauth_router, "FLOWS", store)
    return store


@pytest.fixture
def token_endpoint(monkeypatch):
    endpoint = TokenEndpoint()

    def fake_post(url, data, timeout=None, **kwargs):
        assert url == oauth_google.TOKEN_ENDPOINT
        endpoint.calls.append(dict(data))
        if endpoint.raises is not None:
            raise endpoint.raises
        return endpoint.reply

    monkeypatch.setattr(oauth_google.requests, "post", fake_post)
    return endpoint


@pytest.fixture
def fake_msal(monkeypatch):
    """Only the flow start: the Microsoft test that needs it ends in a provider error, before any exchange."""

    class FakeApp:
        count = 0

        def __init__(self, client_id, authority=None, token_cache=None, **kwargs):
            pass

        def initiate_auth_code_flow(self, scopes, redirect_uri=None, **kwargs):
            FakeApp.count += 1
            state = f"ms-state-{FakeApp.count}-Wq82nKd"
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

        def get_accounts(self):
            return []

    monkeypatch.setattr(oauth_m365.msal, "PublicClientApplication", FakeApp)


@pytest.fixture
def client(env, flows, token_endpoint, fake_msal, caplog) -> TestClient:
    caplog.set_level(logging.DEBUG)
    app = FastAPI()
    install_security(app, allowed_origins=[UI])
    app.include_router(oauth_router.router, prefix="/api")
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def gmail(env):
    return connections.create("gmail", {"label": "personal", "client_id": G_CLIENT_ID, "client_secret": G_CLIENT_SECRET})


@pytest.fixture
def m365(env):
    return connections.create("m365", {"alias": "acme", "tenant_id": TENANT_ID, "client_id": M_CLIENT_ID})


class Started:
    def __init__(self, flow_id: str, state: str, nonce: str, verifier: str):
        self.flow_id = flow_id
        self.state = state
        self.nonce = nonce
        self.verifier = verifier

    @property
    def cookie(self) -> dict[str, str]:
        return {"Cookie": f"ss_oauth_{self.flow_id}={self.nonce}"}


def _start(client, flows, provider="google", connection_id=G_ID, mode="callback") -> Started:
    resp = client.post(
        f"/api/oauth/{provider}/start", json={"connection_id": connection_id, "mode": mode}, headers=WRITE
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    cookie = resp.headers.get_list("set-cookie")[0]
    nonce = cookie.split("=", 1)[1].split(";", 1)[0]
    client.cookies.clear()
    verifier = flows._flows[body["flow_id"]].payload["code_verifier"]
    return Started(body["flow_id"], parse_qs(urlsplit(body["auth_url"]).query)["state"][0], nonce, verifier)


def _callback(client, provider, started: Started, **params):
    return client.get(
        f"/api/oauth/callback/{provider}", params=params, headers=started.cookie, follow_redirects=False
    )


def _paste(client, provider, started: Started, url: str):
    return client.post(
        f"/api/oauth/{provider}/paste", json={"flow_id": started.flow_id, "url": url}, headers={**WRITE, **started.cookie}
    )


def _paste_url(state, code=CODE):
    return f"http://127.0.0.1:8765/?state={state}&code={code}&scope=x"


def _status(client, flow_id, headers=None):
    return client.get(f"/api/oauth/flows/{flow_id}", headers=headers or {})


def _everything(resp) -> str:
    headers = "\n".join(f"{k}: {v}" for k, v in resp.headers.multi_items())
    return f"{resp.status_code}\n{headers}\n{resp.text}"


def _assert_clean(resp, started: Started) -> None:
    text = _everything(resp)
    for value in (*SECRETS, started.state, started.nonce, started.verifier, started.flow_id):
        assert value not in text, "a secret leaked"


def _later(monkeypatch, seconds: int) -> None:
    moment = oauth_flows.utcnow() + timedelta(seconds=seconds)
    monkeypatch.setattr(oauth_flows, "utcnow", lambda: moment)


# ---- pending / ok / error ----


@pytest.mark.parametrize("mode", ["callback", "paste_back"])
def test_status_is_pending_right_after_start(client, gmail, flows, mode):
    started = _start(client, flows, mode=mode)

    resp = _status(client, started.flow_id, started.cookie)

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}
    assert resp.headers["cache-control"] == "no-store"
    _assert_clean(resp, started)


def test_status_is_ok_after_a_successful_callback_and_the_redirect_is_unchanged(client, gmail, token_endpoint, flows):
    started = _start(client, flows)

    redirect = _callback(client, "google", started, state=started.state, code=CODE)
    resp = _status(client, started.flow_id, started.cookie)

    assert redirect.status_code == 302 and redirect.headers["location"] == SUCCESS
    assert redirect.text == "" and redirect.headers["cache-control"] == "no-store"
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    _assert_clean(resp, started)
    assert len(flows) == 0


def test_status_is_ok_after_a_successful_paste(client, gmail, token_endpoint, flows):
    started = _start(client, flows, mode="paste_back")

    pasted = _paste(client, "google", started, _paste_url(started.state))
    resp = _status(client, started.flow_id, started.cookie)

    assert pasted.status_code == 200 and pasted.json()["status"] == "connected"
    assert resp.json() == {"status": "ok"}
    _assert_clean(resp, started)


def test_status_is_error_with_the_redirects_reason_after_a_provider_error_callback(client, gmail, flows):
    started = _start(client, flows)

    redirect = _callback(client, "google", started, state=started.state, error="access_denied", error_description="no")
    resp = _status(client, started.flow_id, started.cookie)

    assert redirect.status_code == 302
    assert redirect.headers["location"] == f"{UI}/connections?oauth=error&reason=denied"
    assert resp.status_code == 200
    assert resp.json() == {"status": "error", "reason": "denied"}
    assert resp.json()["reason"] in oauth_router._REASONS
    _assert_clean(resp, started)


def test_status_is_error_after_a_microsoft_provider_error_callback(client, m365, flows):
    started = _start(client, flows, "microsoft", M_ID)

    redirect = _callback(client, "microsoft", started, state=started.state, error="access_denied")
    resp = _status(client, started.flow_id, started.cookie)

    assert redirect.headers["location"] == f"{UI}/connections?oauth=error&reason=denied"
    assert resp.json() == {"status": "error", "reason": "denied"}
    _assert_clean(resp, started)


def test_status_is_error_after_a_failed_paste(client, gmail, token_endpoint, flows):
    started = _start(client, flows, mode="paste_back")
    other = "state-from-another-sign-in"

    pasted = _paste(client, "google", started, _paste_url(other))
    resp = _status(client, started.flow_id, started.cookie)

    assert pasted.status_code == 400
    assert resp.json() == {"status": "error", "reason": "invalid_request"}


def test_a_provider_failure_still_ends_as_error(client, gmail, token_endpoint, flows):
    token_endpoint.reply = _FakeResponse(400, {"error": "invalid_grant"})
    started = _start(client, flows)

    redirect = _callback(client, "google", started, state=started.state, code=CODE)
    resp = _status(client, started.flow_id, started.cookie)

    assert redirect.headers["location"] == f"{UI}/connections?oauth=error&reason=exchange_failed"
    assert resp.json() == {"status": "error", "reason": "exchange_failed"}
    _assert_clean(resp, started)


def test_an_unexpected_exception_ends_as_error_failed_on_both_routes(client, gmail, token_endpoint, flows):
    token_endpoint.raises = RuntimeError(f"boom {CODE}")
    by_callback = _start(client, flows)
    by_paste = _start(client, flows, mode="paste_back")

    redirect = _callback(client, "google", by_callback, state=by_callback.state, code=CODE)
    with pytest.raises(RuntimeError):
        _paste(client, "google", by_paste, _paste_url(by_paste.state))

    assert redirect.headers["location"] == f"{UI}/connections?oauth=error&reason=failed"
    for started in (by_callback, by_paste):
        resp = _status(client, started.flow_id, started.cookie)
        assert resp.json() == {"status": "error", "reason": "failed"}
        assert CODE not in _everything(resp)


def test_attempts_that_do_not_consume_the_flow_leave_it_pending(client, gmail, token_endpoint, flows):
    callback_flow = _start(client, flows)
    paste_flow = _start(client, flows, mode="paste_back")
    intruder = {"Cookie": f"ss_oauth_{callback_flow.flow_id}=not-the-nonce"}

    bad_nonce = client.get(
        "/api/oauth/callback/google", params={"state": callback_flow.state, "code": CODE},
        headers=intruder, follow_redirects=False,
    )
    unusable = _paste(client, "google", paste_flow, "not a url")

    assert bad_nonce.status_code == 302 and unusable.status_code == 400
    assert token_endpoint.calls == []
    for started in (callback_flow, paste_flow):
        assert _status(client, started.flow_id, started.cookie).json() == {"status": "pending"}


def test_a_replayed_callback_does_not_flip_an_ok_outcome_to_error(client, gmail, token_endpoint, flows):
    started = _start(client, flows)
    assert _callback(client, "google", started, state=started.state, code=CODE).headers["location"] == SUCCESS

    replay = _callback(client, "google", started, state=started.state, code=CODE)
    replayed_error = _callback(client, "google", started, state=started.state, error="access_denied")
    resp = _status(client, started.flow_id, started.cookie)

    assert replay.headers["location"] == f"{UI}/connections?oauth=error&reason=invalid_request"
    assert replayed_error.headers["location"] == f"{UI}/connections?oauth=error&reason=invalid_request"
    assert resp.json() == {"status": "ok"}
    assert len(token_endpoint.calls) == 1


# ---- expiry ----


def test_status_is_expired_after_the_ttl(client, gmail, flows, monkeypatch):
    started = _start(client, flows)
    _later(monkeypatch, oauth_flows.DEFAULT_TTL_SECONDS + 5)

    resp = _status(client, started.flow_id, started.cookie)

    assert resp.status_code == 200 and resp.json() == {"status": "expired"}
    _assert_clean(resp, started)


def test_a_finished_flows_outcome_is_gone_at_its_original_expiry(client, gmail, token_endpoint, flows, monkeypatch):
    started = _start(client, flows)
    _callback(client, "google", started, state=started.state, code=CODE)
    assert _status(client, started.flow_id, started.cookie).json() == {"status": "ok"}
    _later(monkeypatch, oauth_flows.DEFAULT_TTL_SECONDS + 5)

    resp = _status(client, started.flow_id, started.cookie)

    assert resp.status_code == 404


# ---- no oracle ----


def test_unknown_flow_wrong_cookie_and_missing_cookie_answer_identical_404s(client, gmail, token_endpoint, flows):
    pending = _start(client, flows)
    finished = _start(client, flows)
    _callback(client, "google", finished, state=finished.state, code=CODE)
    other = _start(client, flows)

    answers = [
        _status(client, "no-such-flow-id", {"Cookie": "ss_oauth_no-such-flow-id=" + pending.nonce}),
        _status(client, "no-such-flow-id"),
        _status(client, pending.flow_id),
        _status(client, pending.flow_id, {"Cookie": f"ss_oauth_{pending.flow_id}=wrong"}),
        _status(client, pending.flow_id, {"Cookie": f"ss_oauth_{pending.flow_id}="}),
        _status(client, pending.flow_id, other.cookie),
        _status(client, pending.flow_id, {"Cookie": f"ss_oauth_{pending.flow_id}={other.nonce}"}),
        _status(client, finished.flow_id),
        _status(client, finished.flow_id, {"Cookie": f"ss_oauth_{finished.flow_id}={pending.nonce}"}),
    ]

    assert {a.status_code for a in answers} == {404}
    assert len({a.content for a in answers}) == 1
    assert len({tuple(sorted(a.headers.items())) for a in answers}) == 1
    body = answers[0].json()
    assert body == {"detail": {"code": "unknown_flow", "message": "Unknown sign-in flow"}}


def test_a_flow_ids_own_nonce_never_opens_another_flow(client, gmail, flows):
    first, second = _start(client, flows), _start(client, flows)

    resp = _status(client, second.flow_id, {"Cookie": f"ss_oauth_{first.flow_id}={first.nonce}"})

    assert resp.status_code == 404


def test_status_needs_no_request_guard_header_and_ignores_other_methods(client, gmail, flows):
    started = _start(client, flows)

    assert _status(client, started.flow_id, started.cookie).status_code == 200
    assert client.post(f"/api/oauth/flows/{started.flow_id}", headers={**WRITE, **started.cookie}).status_code == 405


# ---- bounded table ----


def test_the_outcome_table_stays_bounded(client, gmail, flows):
    last = None
    for _ in range(150):
        last = _start(client, flows)
        _callback(client, "google", last, state=last.state, error="access_denied")

    assert len(flows._outcomes) <= oauth_flows.MAX_OUTCOMES == 100
    assert len(flows) == 0
    assert last is not None
    assert _status(client, last.flow_id, last.cookie).json() == {"status": "error", "reason": "denied"}


# ---- store level ----


def _create(store, state="s-1", now=None):
    return store.create("google", G_ID, "callback", state, "https://hook.example.com/cb", {"code_verifier": VERIFIER}, now=now)


def test_store_reports_pending_while_the_provider_call_is_still_running():
    store = FlowStore()
    flow, nonce = _create(store)

    store.consume(state="s-1", nonce=nonce, provider="google")

    assert store.status(flow.flow_id, nonce) == ("pending", None)
    assert store.status(flow.flow_id, "wrong") is None
    assert store.status(flow.flow_id, None) is None


def test_store_outcome_is_settled_once_and_the_first_write_wins():
    store = FlowStore()
    flow, nonce = _create(store)
    store.consume(state="s-1", nonce=nonce)

    store.record_outcome(flow.flow_id, "ok")
    store.record_outcome(flow.flow_id, "error", "failed")

    assert store.status(flow.flow_id, nonce) == ("ok", None)


def test_store_record_outcome_ignores_a_flow_that_was_never_consumed():
    store = FlowStore()
    flow, nonce = _create(store)

    store.record_outcome(flow.flow_id, "error", "failed")
    store.record_outcome("no-such-flow", "ok")

    assert store.status(flow.flow_id, nonce) == ("pending", None)
    assert store.status("no-such-flow", nonce) is None
    with pytest.raises(ValueError):
        store.record_outcome(flow.flow_id, "pending")


def test_store_outcome_never_outlives_the_flows_expiry():
    store = FlowStore()
    now = oauth_flows.utcnow()
    flow, nonce = _create(store, now=now)
    store.consume(state="s-1", nonce=nonce, now=now)
    store.record_outcome(flow.flow_id, "ok")

    assert store.status(flow.flow_id, nonce, now=now + timedelta(seconds=899)) == ("ok", None)
    assert store.status(flow.flow_id, nonce, now=now + timedelta(seconds=900)) is None
    _create(store, state="s-2", now=now + timedelta(seconds=901))
    assert store._outcomes == {}


def test_store_flow_id_for_state_does_not_consume_and_ignores_junk():
    store = FlowStore()
    flow, nonce = _create(store)

    assert store.flow_id_for_state("s-1") == flow.flow_id
    assert store.flow_id_for_state("s-1") == flow.flow_id
    assert store.flow_id_for_state("other") is None
    assert store.flow_id_for_state(None) is None
    assert store.flow_id_for_state("") is None
    assert store.flow_id_for_state("ключ") is None
    assert len(store) == 1
    assert store.status(flow.flow_id, nonce) == ("pending", None)


def test_the_store_and_its_records_keep_secrets_out_of_repr():
    store = FlowStore()
    flow, nonce = _create(store)
    store.consume(state="s-1", nonce=nonce)

    shown = repr(store._outcomes)

    assert nonce not in shown and flow.nonce_hash not in shown and re.search(r"pending", shown)
