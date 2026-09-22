"""
A Gmail connection must be creatable BEFORE its first sign-in (check-report finding 1).

Browser sign-in needs an existing connection, and the create route only accepts an id that
health.known_sources() lists. known_sources() anchors Gmail on GMAIL_<L>_REFRESH_TOKEN, so a
connection with no token yet used to be invisible and every create answered 503 store_inactive.
The live overlay (materialize(pending_gmail_token=True)) now emits that key EMPTY until sign-in stores the token.

Every test drives the REAL app: the real lifespan (TestClient as a context manager), security
middleware, routers, connection store, env overlay, runner and Fernet vault, against a temp database
and a temp ROOT holding a temp .env. Only true externals are replaced, and each is recorded:

- requests.post / requests.get: Google's token endpoint and the Gmail profile call. A call the test
  did not declare fails it, which is how "no network call" is proven;
- runner.dispatch: the collectors' network layer, so "the run skips it" is provable;
- api.main.start_scheduler: the APScheduler thread and clock.

Every response (body and headers) is swept for every secret value the scenario used, and the
persisted SQLite file is searched for the plaintext. All secret values are invented.
"""
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as api_main  # noqa: E402
from pipeline import config_store, connections, crypto, db, health, oauth_gmail, runner  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
PROFILE_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

CLIENT_ID = "pending-client.apps.example.com"
CLIENT_SECRET = "pending-SECRET-client-Zk4"
REFRESH_TOKEN = "pending-SECRET-refresh-Vq7"
AUTH_CODE = "pending-SECRET-code-Lm2"
ACCESS_TOKEN = "pending-SECRET-access-Rt9"
SECRETS = (CLIENT_SECRET, REFRESH_TOKEN, AUTH_CODE, ACCESS_TOKEN)

GMAIL_BODY = {"kind": "gmail", "label": "fresh", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Network:
    """The only outbound HTTP the flows make; every call is recorded, an undeclared one fails the test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.unexpected: list[str] = []
        self.dispatched: list[str] = []

    def post(self, url: str, data: Optional[dict] = None, **_: Any) -> FakeResponse:
        payload = dict(data or {})
        self.calls.append(("POST", url, payload))
        if url != TOKEN_ENDPOINT:
            self.unexpected.append(url)
            return FakeResponse({}, 500)
        if payload.get("grant_type") == "authorization_code":
            return FakeResponse(
                {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN, "scope": health.GMAIL_SCOPE}
            )
        return FakeResponse({"access_token": ACCESS_TOKEN, "scope": health.GMAIL_SCOPE, "expires_in": 3600})

    def get(self, url: str, **_: Any) -> FakeResponse:
        self.calls.append(("GET", url, {}))
        if url != PROFILE_ENDPOINT:
            self.unexpected.append(url)
            return FakeResponse({}, 500)
        return FakeResponse({"emailAddress": "fresh@example.com"})

    def dispatch(self, source: str, *_: Any, **__: Any) -> Any:
        self.dispatched.append(source)
        raise AssertionError(f"collector dispatched for {source}")


@pytest.fixture
def net(monkeypatch, tmp_path) -> Iterator[Network]:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(api_main, "start_scheduler", lambda: None)
    (tmp_path / ".env").write_text("")
    network = Network()
    monkeypatch.setattr(requests, "post", network.post)
    monkeypatch.setattr(requests, "get", network.get)
    monkeypatch.setattr(runner, "dispatch", network.dispatch)
    yield network
    connections.set_vault(None)
    health.set_env_overlay_provider(None, None)
    assert network.unexpected == []


@contextmanager
def running_app(monkeypatch, *, key: bool = True) -> Iterator[TestClient]:
    if key:
        monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", crypto.generate_key())
    with TestClient(api_main.app, headers=WRITE) as client:
        yield client


def assert_no_secret(*responses: Any) -> None:
    for resp in responses:
        blob = resp.text + str(dict(resp.headers))
        for secret in SECRETS:
            assert secret not in blob


def rows(client: TestClient) -> dict[str, dict]:
    resp = client.get("/api/connections")
    assert resp.status_code == 200
    assert_no_secret(resp)
    return {row["id"]: row for row in resp.json()}


def test_create_without_refresh_token_is_201_inactive_and_listed(net, monkeypatch, tmp_path):
    with running_app(monkeypatch) as client:
        created = client.post("/api/connections", json=GMAIL_BODY)
        assert created.status_code == 201
        assert created.json()["id"] == "gmail_fresh"
        assert created.json()["active"] is False
        assert_no_secret(created)

        listed = rows(client)["gmail_fresh"]
        assert listed["secrets_set"] == ["client_secret"]
        assert listed["active"] is False
        assert "gmail_fresh" in health.known_sources()
        assert health.gmail_accounts()["fresh"] is None
    assert CLIENT_SECRET.encode() not in (tmp_path / "test.db").read_bytes()
    assert net.calls == []


def test_test_button_says_not_signed_in_without_a_network_call(net, monkeypatch):
    with running_app(monkeypatch) as client:
        assert client.post("/api/connections", json=GMAIL_BODY).status_code == 201
        resp = client.post("/api/connections/gmail_fresh/test")
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"
        assert "Not signed in yet" in resp.json()["detail"]
        assert_no_secret(resp)
    assert net.calls == []


def test_check_gmail_guards_a_missing_token_before_any_network_call(net, monkeypatch):
    with running_app(monkeypatch) as client:
        assert client.post("/api/connections", json=GMAIL_BODY).status_code == 201
        result = health.check_gmail("fresh")
        assert (result.status, result.detail) == ("error", "Not signed in yet: use Sign in")
        with pytest.raises(RuntimeError, match="Not signed in yet"):
            health.gmail_token_response("fresh")
    assert net.calls == []


def test_new_account_path_create_sign_in_then_test_ok(net, monkeypatch, tmp_path):
    with running_app(monkeypatch) as client:
        assert client.post("/api/connections", json=GMAIL_BODY).status_code == 201

        start = client.post("/api/oauth/google/start", json={"connection_id": "gmail_fresh", "mode": "paste_back"})
        assert start.status_code == 200
        assert_no_secret(start)
        auth = urlsplit(start.json()["auth_url"])
        query = parse_qs(auth.query)
        assert query["client_id"] == [CLIENT_ID]
        state = query["state"][0]
        flow_id = start.json()["flow_id"]
        assert client.cookies.get(f"ss_oauth_{flow_id}")

        redirect = f"{oauth_gmail.PASTE_BACK_REDIRECT_URI}/?code={AUTH_CODE}&state={state}"
        paste = client.post("/api/oauth/google/paste", json={"flow_id": flow_id, "url": redirect})
        assert paste.status_code == 200, paste.text
        assert paste.json()["status"] == "connected"
        assert_no_secret(paste)
        exchange = [payload for method, url, payload in net.calls if payload.get("grant_type") == "authorization_code"]
        assert len(exchange) == 1 and exchange[0]["client_id"] == CLIENT_ID

        listed = rows(client)["gmail_fresh"]
        assert set(listed["secrets_set"]) == {"client_secret", "refresh_token"}
        assert "gmail_fresh" in health.known_sources()
        assert health.gmail_accounts()["fresh"] == REFRESH_TOKEN

        tested = client.post("/api/connections/gmail_fresh/test")
        assert tested.status_code == 200
        assert tested.json()["status"] == "ok"
        assert "fresh@example.com" in tested.json()["detail"]
        assert_no_secret(tested)
    persisted = (tmp_path / "test.db").read_bytes()
    assert all(secret.encode() not in persisted for secret in SECRETS)


def test_delete_removes_the_pending_connection_from_known_sources(net, monkeypatch):
    with running_app(monkeypatch) as client:
        assert client.post("/api/connections", json=GMAIL_BODY).status_code == 201
        assert "gmail_fresh" in health.known_sources()
        assert client.delete("/api/connections/gmail_fresh").status_code == 204
        assert "gmail_fresh" not in health.known_sources()
        assert "gmail_fresh" not in rows(client)
        assert "fresh" not in health.gmail_accounts()


def test_other_kinds_still_create_201(net, monkeypatch):
    bodies = [
        {"kind": "slack", "label": "team", "token": "xoxb-pending-SECRET-slack"},
        {"kind": "zoom", "account_id": "acct_Pending1", "client_id": "zoomPending", "client_secret": "zoom-pending-SECRET"},
        {
            "kind": "m365",
            "alias": "contoso",
            "tenant_id": "11111111-2222-3333-4444-555555555555",
            "client_id": "66666666-7777-8888-9999-000000000000",
        },
    ]
    with running_app(monkeypatch) as client:
        for body in bodies:
            resp = client.post("/api/connections", json=body)
            assert resp.status_code == 201, (body["kind"], resp.text)
            assert resp.json()["active"] is False
            assert "SECRET" not in resp.text


def test_a_pending_gmail_source_toggled_active_is_never_collected(net, monkeypatch):
    with running_app(monkeypatch) as client:
        assert client.post("/api/connections", json=GMAIL_BODY).status_code == 201
        config_store.set_source_active("gmail_fresh", True)

        run = runner.execute_run("manual", only="gmail_fresh")

        assert net.dispatched == []
        assert run.status != "running"
        latest = {row.source: row for row in db.latest_source_health()}
        assert latest["gmail_fresh"].status == "error"
        assert "Not signed in yet" in (latest["gmail_fresh"].detail or "")
    assert net.calls == []


def test_secret_values_and_redaction_ignore_empty_strings(net, monkeypatch):
    with running_app(monkeypatch) as client:
        assert client.post("/api/connections", json=GMAIL_BODY).status_code == 201
        known = connections.secret_values()
        assert CLIENT_SECRET in known
        assert "" not in known
        # An empty needle would match everywhere and blank the whole text.
        assert health._env()["GMAIL_FRESH_REFRESH_TOKEN"] == ""
        assert runner._scrub("plain detail text", known) == "plain detail text"


def test_env_only_install_is_unchanged(net, monkeypatch, tmp_path):
    real_token = "env-only-SECRET-refresh"
    (tmp_path / ".env").write_text(
        "GMAIL_CLIENT_ID=shared.apps.example.com\n"
        "GMAIL_CLIENT_SECRET=env-only-SECRET-client\n"
        f"GMAIL_PERSONAL_REFRESH_TOKEN={real_token}\n"
        "GMAIL_BLANK_REFRESH_TOKEN=\n"
    )
    with running_app(monkeypatch, key=False):
        assert connections.get_vault() is None
        assert health.gmail_accounts() == {"blank": None, "personal": real_token}
        sources = health.known_sources()
        assert "gmail_personal" in sources and "gmail_blank" in sources
        assert "gmail_client" not in sources
    assert net.calls == []
