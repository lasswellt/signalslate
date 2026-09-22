"""
End-to-end backend regression for the management UI (docs/_research/2026-09-21_management-ui.md).

Every test drives the REAL app object: the real lifespan (TestClient as a context manager), the
real security middleware, every router, the real connection store, the real env overlay, the real
runner and the real Fernet vault, against a temp database, a temp ROOT holding a temp .env and a
temp config file. Only true externals are replaced:

- requests.post, the Slack auth.test call (the only outbound HTTP this flow makes);
- runner.dispatch and collect.dispatch, the collectors' own network layer (runs and dry runs);
- the config router's reschedule and start_scheduler: the APScheduler thread and clock. A job added
  to a scheduler that was never started has no next_run_time, so the real reschedule would make
  GET /status raise for every later request in the process.

Every response of every test goes through one recorder (headers as well as body). At the end of a
test the recorder is swept for every secret value the scenario used, for the encryption key, and
for every ciphertext the store ever held (snapshotted after each request, so a ciphertext that a
later PATCH replaced is still looked for), and the persisted SQLite and config files are searched
for the plaintext. All secret values are invented.
"""
import logging
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import requests
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, col, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as api_main  # noqa: E402
from api.routers import collectors as collectors_router  # noqa: E402
from api.routers import config as config_router  # noqa: E402
from pipeline import collect, config_store, connections, crypto, db, health, oauth_zoom, runner  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.collectors import CollectionResult, Item  # noqa: E402
from pipeline.redact import MARKER  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}
ALLOWED_ORIGIN = "http://localhost:3000"
FOREIGN_ORIGIN = "https://evil.example.com"
SLACK_AUTH_TEST = "https://slack.com/api/auth.test"

# No secret is a substring of another, so a hit always names the value that leaked.
SLACK_WORK_TOKEN = "xoxb-e2e-SECRET-work"
SLACK_OLD_TOKEN = "xoxb-e2e-SECRET-old"
SLACK_NEW_TOKEN = "xoxb-e2e-SECRET-new"
ZOOM_SECRET = "zoom-e2e-SECRET-orig"
ZOOM_SECRET_EDITED = "zoom-e2e-SECRET-edited"
GMAIL_SECRET = "gmail-e2e-SECRET-client"
GMAIL_REFRESH_OLD = "gmail-refresh-e2e-SECRET-old"
GMAIL_REFRESH_NEW = "gmail-refresh-e2e-SECRET-new"
STALE_SLACK = "xoxb-e2e-STALE-slack"
STALE_ZOOM = "zoom-e2e-STALE-secret"
PASTED_VALUE = "xoxb-e2e-PASTED-value"
PASTED_KEY = "xoxb-e2e-PASTED-key"
PASTED_PATH = "xoxb-e2e-PASTED-path"

# Zoom OAuth: a distinct prefix from ZOOM_SECRET/STALE_ZOOM above so no value here is ever a
# substring of another secret used elsewhere in this file.
ZOOM_PUBLIC_BASE_URL = "https://hook.e2e.example.com"
ZOOM_OAUTH_CLIENT_ID = "zoom-oauth-client-e2e"
ZOOM_OAUTH_CLIENT_SECRET = "zoom-oauth-e2e-SECRET-client"
ZOOM_AUTH_CODE = "zoom-oauth-e2e-SECRET-authcode"
ZOOM_REFRESH_INITIAL = "zoom-oauth-e2e-SECRET-refresh-initial"
ZOOM_REFRESH_ROTATED = "zoom-oauth-e2e-SECRET-refresh-rotated"
ZOOM_ACCESS_INITIAL = "zoom-oauth-e2e-SECRET-access-initial"
ZOOM_ACCESS_ROTATED = "zoom-oauth-e2e-SECRET-access-rotated"

SECRETS: dict[str, str] = {
    "slack work token": SLACK_WORK_TOKEN,
    "slack old token": SLACK_OLD_TOKEN,
    "slack new token": SLACK_NEW_TOKEN,
    "zoom secret": ZOOM_SECRET,
    "zoom secret edited": ZOOM_SECRET_EDITED,
    "gmail client secret": GMAIL_SECRET,
    "gmail refresh old": GMAIL_REFRESH_OLD,
    "gmail refresh new": GMAIL_REFRESH_NEW,
    "stale env slack token": STALE_SLACK,
    "stale env zoom secret": STALE_ZOOM,
    "pasted value": PASTED_VALUE,
    "pasted key name": PASTED_KEY,
    "pasted path": PASTED_PATH,
    "zoom oauth client secret": ZOOM_OAUTH_CLIENT_SECRET,
    "zoom oauth auth code": ZOOM_AUTH_CODE,
    "zoom oauth refresh initial": ZOOM_REFRESH_INITIAL,
    "zoom oauth refresh rotated": ZOOM_REFRESH_ROTATED,
    "zoom oauth access initial": ZOOM_ACCESS_INITIAL,
    "zoom oauth access rotated": ZOOM_ACCESS_ROTATED,
}

GMAIL_CLIENT_ID = "gmail-client-e2e.apps.example.com"
ENV_FILE = (
    f"SLACK_WORK_TOKEN={SLACK_WORK_TOKEN}\n"
    "ZOOM_ACCOUNT_ID=acct_E2eExample\n"
    "ZOOM_CLIENT_ID=zoomClientE2e\n"
    f"ZOOM_CLIENT_SECRET={ZOOM_SECRET}\n"
    f"GMAIL_CLIENT_ID={GMAIL_CLIENT_ID}\n"
    f"GMAIL_CLIENT_SECRET={GMAIL_SECRET}\n"
    f"GMAIL_PERSONAL_REFRESH_TOKEN={GMAIL_REFRESH_OLD}\n"
)
# What .env says after the operator edits it while the store already owns these connections.
CHANGED_ENV_FILE = (
    f"SLACK_WORK_TOKEN={STALE_SLACK}\n"
    "ZOOM_ACCOUNT_ID=acct_FromEnvChanged\n"
    "ZOOM_CLIENT_ID=zoomClientFromEnvChanged\n"
    f"ZOOM_CLIENT_SECRET={STALE_ZOOM}\n"
    "GMAIL_CLIENT_ID=gmail-client-env-changed.apps.example.com\n"
    f"GMAIL_CLIENT_SECRET={GMAIL_SECRET}\n"
    f"GMAIL_PERSONAL_REFRESH_TOKEN={GMAIL_REFRESH_OLD}\n"
)

CONNECTION_FIELDS = {"id", "kind", "label", "origin", "config", "secrets_set", "active", "health"}

# Like ENV_FILE, but with no ZOOM_* line: the store starts with no Zoom connection at all, so the
# UI can create one as id "zoom" (a Zoom connection is always a singleton) without hitting the
# duplicate_connection 409 the s2s-seeded ENV_FILE above would cause.
ZOOM_OAUTH_ENV_FILE = (
    f"PUBLIC_BASE_URL={ZOOM_PUBLIC_BASE_URL}\n"
    f"SLACK_WORK_TOKEN={SLACK_WORK_TOKEN}\n"
    f"GMAIL_CLIENT_ID={GMAIL_CLIENT_ID}\n"
    f"GMAIL_CLIENT_SECRET={GMAIL_SECRET}\n"
    f"GMAIL_PERSONAL_REFRESH_TOKEN={GMAIL_REFRESH_OLD}\n"
)


# --- recording ------------------------------------------------------------------------------------


@dataclass
class Record:
    method: str
    url: str
    status: int
    blob: str  # status, every header, then the body


@dataclass
class Capture:
    """Every response of a test, and every ciphertext the store held while it ran."""

    home: Path
    records: list[Record] = field(default_factory=list)
    ciphertexts: set[str] = field(default_factory=set)
    extra: dict[str, str] = field(default_factory=dict)
    swept: bool = False

    def snapshot_ciphertexts(self) -> None:
        with db.get_session() as session:
            for value in session.exec(select(db.Connection.secret_ciphertext)).all():
                if value:
                    self.ciphertexts.add(value)

    def record(self, method: str, url: str, response: httpx.Response) -> None:
        headers = "\n".join(f"{name}: {value}" for name, value in response.headers.multi_items())
        body = response.content.decode("utf-8", errors="replace")
        self.records.append(Record(method, url, response.status_code, f"{response.status_code}\n{headers}\n\n{body}"))
        self.snapshot_ciphertexts()

    def needles(self) -> dict[str, str]:
        return {**SECRETS, **self.extra}

    def assert_absent_since(self, start: int, secret: str, label: str) -> None:
        hits = [f"{r.method} {r.url} ({r.status})" for r in self.records[start:] if secret in r.blob]
        assert not hits, f"{label} appeared in: {hits}"

    def assert_clean(self, *, files: bool = False) -> None:
        """Nothing secret in any recorded response; with files=True, nor in the persisted files."""
        self.snapshot_ciphertexts()
        needles = self.needles()
        cipher = {f"ciphertext #{i}": value for i, value in enumerate(sorted(self.ciphertexts))}
        problems: list[str] = []
        for record in self.records:
            for label, value in {**needles, **cipher}.items():
                if value in record.blob:
                    problems.append(f"{record.method} {record.url} ({record.status}) leaked {label}")
            # Any Fernet token, not only the ones this test happened to store.
            if "gAAAAA" in record.blob:
                problems.append(f"{record.method} {record.url} ({record.status}) carried a Fernet token")
        assert not problems, problems

        if files:
            for name in ("test.db", "test.db-wal", "test.db-journal", "config.json"):
                path = self.home / name
                if not path.exists():
                    continue
                data = path.read_bytes()
                found = [label for label, value in needles.items() if value.encode() in data]
                assert not found, f"plaintext in {name}: {found}"
        self.swept = True


class Recorder:
    """The TestClient surface the tests use, recording every response."""

    def __init__(self, client: TestClient, capture: Capture) -> None:
        self._client = client
        self.capture = capture

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, url, **kwargs)
        self.capture.record(method, url, response)
        return response

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("OPTIONS", url, **kwargs)

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies


# --- the externals --------------------------------------------------------------------------------


class _SlackReply:
    def __init__(self, ok: bool) -> None:
        self._ok = ok
        self.headers = {"x-oauth-scopes": ", ".join(sorted(health.SLACK_SCOPES))}

    def json(self) -> dict[str, Any]:
        if self._ok:
            return {"ok": True, "user": "alice", "team": "example"}
        return {"ok": False, "error": "invalid_auth"}


class _FakeZoomResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


@dataclass
class ZoomTokenStub:
    """
    Stands in for https://zoom.us/oauth/token: the authorization_code exchange (finish_callback)
    and the refresh_token rotation (health.check_zoom -> oauth_zoom.refresh). Zoom always rotates
    the refresh token on use, so every successful call hands back a value distinct from what it
    was given.
    """

    granted_scope: str = field(default_factory=lambda: " ".join(sorted(health.ZOOM_USER_SCOPES | {health.ZOOM_TRANSCRIPT_SCOPE})))
    refresh_status: int = 200
    refresh_error: str = "invalid_grant"
    exchange_calls: list[dict] = field(default_factory=list)
    refresh_calls: list[dict] = field(default_factory=list)

    def post(self, data: dict, auth: Any) -> _FakeZoomResponse:
        grant = data.get("grant_type")
        if grant == "authorization_code":
            self.exchange_calls.append({"data": data, "auth": auth})
            return _FakeZoomResponse(
                200,
                {
                    "refresh_token": ZOOM_REFRESH_INITIAL,
                    "access_token": ZOOM_ACCESS_INITIAL,
                    "expires_in": 3600,
                    "scope": self.granted_scope,
                },
            )
        if grant == "refresh_token":
            self.refresh_calls.append({"data": data, "auth": auth})
            if self.refresh_status != 200:
                return _FakeZoomResponse(self.refresh_status, {"error": self.refresh_error})
            return _FakeZoomResponse(
                200,
                {
                    "refresh_token": ZOOM_REFRESH_ROTATED,
                    "access_token": ZOOM_ACCESS_ROTATED,
                    "expires_in": 3600,
                    "scope": self.granted_scope,
                },
            )
        raise AssertionError(f"unexpected zoom grant_type {grant!r}")


class Network:
    """
    Stands in for Slack, Zoom's OAuth token endpoint, and for the collectors' network layer. It
    reads the token from the live env exactly as a collector would, so a token that never reached
    the overlay fails the test.
    """

    def __init__(self) -> None:
        self.valid_tokens: Optional[set[str]] = None  # None: Slack accepts any token
        self.slack_tokens: list[str] = []
        self.unexpected: list[str] = []
        self.dispatched: list[tuple[str, Optional[str]]] = []
        self.zoom: Optional[ZoomTokenStub] = None

    def post(self, url: str, *args: Any, **kwargs: Any) -> Any:
        if url == SLACK_AUTH_TEST:
            token = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
            self.slack_tokens.append(token)
            return _SlackReply(self.valid_tokens is None or token in self.valid_tokens)
        if url == oauth_zoom.TOKEN_ENDPOINT and self.zoom is not None:
            return self.zoom.post(kwargs.get("data") or {}, kwargs.get("auth"))
        self.unexpected.append(url)
        raise requests.ConnectionError("no network in tests")

    def dispatch(self, source: str, since: Any, until: Any) -> CollectionResult:
        token = health.slack_workspaces().get(source.removeprefix("slack_")) if source.startswith("slack_") else None
        self.dispatched.append((source, token))
        items = [
            Item(
                item_type="message",
                external_id=f"{source}-{n}",
                occurred_at=utcnow() - timedelta(hours=1, minutes=n),
                payload={"text": f"hello from {source} {n}", "channel": "general"},
            )
            for n in (1, 2)
        ]
        # A collector echoing the credential it used is the leak path the runner's scrubbing exists for.
        return CollectionResult(source, "ok", f"fetched 2 messages as {token}", items)


@dataclass
class World:
    home: Path
    capture: Capture
    net: Network
    monkeypatch: pytest.MonkeyPatch
    rescheduled: list[bool] = field(default_factory=list)

    def enable_key(self) -> str:
        key = crypto.generate_key()
        self.monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", key)
        self.capture.extra["encryption key"] = key
        return key

    def write_env(self, text: str) -> None:
        (self.home / ".env").write_text(text)

    def restart(self) -> None:
        """What a new process starts from: no vault and no overlay (the lifespan also resets both)."""
        connections.set_vault(None)
        health.set_env_overlay_provider(None, None)

    @contextmanager
    def app(self, headers: Optional[dict[str, str]] = None, *, base_url: str = "http://testserver") -> Iterator[Recorder]:
        # base_url="https://..." is what the Zoom OAuth tests need: the nonce cookie is Secure
        # whenever PUBLIC_BASE_URL is https (api.routers.oauth._secure()), and httpx's cookie jar
        # will not replay a Secure cookie back over a plain http:// connection.
        with TestClient(api_main.app, headers=WRITE if headers is None else headers, base_url=base_url) as client:
            yield Recorder(client, self.capture)


@pytest.fixture
def world(monkeypatch, tmp_path) -> Iterator[World]:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(api_main, "start_scheduler", lambda: None)
    (tmp_path / ".env").write_text(ENV_FILE)

    net = Network()
    monkeypatch.setattr(requests, "post", net.post)
    monkeypatch.setattr(runner, "dispatch", net.dispatch)
    monkeypatch.setattr(collect, "dispatch", net.dispatch)
    current = World(tmp_path, Capture(tmp_path), net, monkeypatch)
    monkeypatch.setattr(config_router, "reschedule", lambda: current.rescheduled.append(True))
    collectors_router._jobs.clear()

    yield current

    connections.set_vault(None)
    health.set_env_overlay_provider(None, None)
    collectors_router._jobs.clear()
    assert net.unexpected == [], "a test reached for the network"
    assert current.capture.swept, "the test never swept its recorded responses"


# --- helpers --------------------------------------------------------------------------------------


def rows_by_id(client: Recorder) -> dict[str, dict[str, Any]]:
    resp = client.get("/api/connections")
    assert resp.status_code == 200
    return {row["id"]: row for row in resp.json()}


def collector(client: Recorder, source: str) -> dict[str, Any]:
    resp = client.get("/api/collectors")
    assert resp.status_code == 200
    (row,) = [row for row in resp.json() if row["source"] == source]
    return row


def toggles(client: Recorder) -> dict[str, bool]:
    resp = client.get("/api/config")
    assert resp.status_code == 200
    return resp.json()["active_sources"]


def create_slack(client: Recorder, label: str, token: str) -> httpx.Response:
    return client.post("/api/connections", json={"kind": "slack", "label": label, "token": token})


def activate(client: Recorder, source: str) -> httpx.Response:
    config = client.get("/api/config").json()
    config["active_sources"][source] = True
    return client.put("/api/config", json=config)


def run_one(client: Recorder, source: str) -> None:
    resp = client.post(f"/api/collectors/{source}/run")
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"accepted": True}


def create_test_activate_and_run(client: Recorder, token: str) -> None:
    created = create_slack(client, "e2e", token)
    assert created.status_code == 201, created.text
    assert client.post("/api/connections/slack_e2e/test").json()["status"] == "ok"
    assert activate(client, "slack_e2e").status_code == 200
    run_one(client, "slack_e2e")


def store_state() -> dict[str, Any]:
    """Everything a rejected write must leave alone."""
    with db.get_session() as session:
        rows = [
            (r.id, r.kind, r.label, r.origin, r.config, r.secret_ciphertext, r.updated_at)
            for r in session.exec(select(db.Connection).order_by(col(db.Connection.id))).all()
        ]
        tombstones = sorted(t.id for t in session.exec(select(db.Tombstone)).all())
        runs = len(session.exec(select(db.Run)).all())
        items = len(session.exec(select(db.CollectedItem)).all())
        cursors = sorted(
            (c.source, c.last_success_at, c.consecutive_failures) for c in session.exec(select(db.SourceCursor)).all()
        )
    path = config_store.CONFIG_PATH
    return {
        "connections": rows,
        "tombstones": tombstones,
        "runs": runs,
        "items": items,
        "cursors": cursors,
        "config": path.read_text() if path.exists() else None,
    }


def assert_no_secrets_in_logs(caplog: pytest.LogCaptureFixture, needles: dict[str, str]) -> None:
    """
    The app's own log records carry no secret. httpx/httpcore/asyncio are the test client's own
    request-echo logging (it prints the URL it just called, code and all) and is not the app.
    """
    for record in caplog.records:
        if record.name.startswith(("httpx", "httpcore", "asyncio")):
            continue
        message = record.getMessage()
        for label, value in needles.items():
            assert value not in message, f"{label} leaked into a log record: {record.name} {message[:200]}"


def create_zoom_oauth(client: Recorder) -> dict[str, Any]:
    created = client.post(
        "/api/connections",
        json={
            "kind": "zoom",
            "auth_mode": "oauth",
            "client_id": ZOOM_OAUTH_CLIENT_ID,
            "client_secret": ZOOM_OAUTH_CLIENT_SECRET,
            "include_transcripts": True,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def sign_in_zoom(client: Recorder, code: str = ZOOM_AUTH_CODE) -> str:
    """Runs start + callback for the "zoom" connection; returns the state Zoom's redirect carried."""
    started = client.post("/api/oauth/zoom/start", json={"connection_id": "zoom", "mode": "callback"})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["auth_url"].startswith(f"{oauth_zoom.AUTHORIZE_ENDPOINT}?")
    qs = parse_qs(urlsplit(body["auth_url"]).query)
    assert qs["redirect_uri"] == [f"{ZOOM_PUBLIC_BASE_URL}/api/oauth/callback/zoom"]
    state = qs["state"][0]
    cookie_name = f"ss_oauth_{body['flow_id']}"
    assert cookie_name in client.cookies

    callback = client.get(f"/api/oauth/callback/zoom?code={code}&state={state}", follow_redirects=False)
    assert callback.status_code == 302, callback.text
    assert callback.headers["location"] == f"{ALLOWED_ORIGIN}/connections?oauth=ok"
    assert any(
        line.startswith(f"{cookie_name}=") and "Max-Age=0" in line
        for line in callback.headers.get_list("set-cookie")
    )
    return state


def poll_dry_run(client: Recorder, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while True:
        resp = client.get(f"/api/collectors/dry-run/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] != "running" or time.monotonic() > deadline:
            return body
        time.sleep(0.01)


# --- 1: fresh start seeds .env and lists no secret ------------------------------------------------


def test_fresh_start_seeds_env_connections_and_lists_no_secret_value(world):
    world.enable_key()
    with world.app() as client:
        resp = client.get("/api/connections")
        assert resp.status_code == 200
        listed = resp.json()

        assert [row["id"] for row in listed] == ["zoom", "slack_work", "gmail_personal"]
        rows = {row["id"]: row for row in listed}
        assert {row["origin"] for row in listed} == {"env"}
        # The response model has no field that could carry a value or a ciphertext.
        assert all(set(row) == CONNECTION_FIELDS for row in listed)
        assert rows["zoom"]["secrets_set"] == ["client_secret"]
        assert rows["slack_work"]["secrets_set"] == ["token"]
        assert rows["gmail_personal"]["secrets_set"] == ["client_secret", "refresh_token"]
        assert rows["zoom"]["config"] == {"account_id": "acct_E2eExample", "client_id": "zoomClientE2e"}
        assert rows["slack_work"]["config"] == {"label": "work"}
        assert rows["gmail_personal"]["config"] == {
            "label": "personal",
            "client_id": GMAIL_CLIENT_ID,
            "redirect_mode": "paste_back",
        }
        # A source .env declares is on by default, and nothing has been checked yet.
        assert all(row["active"] is True and row["health"] is None for row in listed)
        for label, value in SECRETS.items():
            assert value not in resp.text, f"{label} in GET /connections"

        assert client.get("/api/system").json()["store_active"] is True
        assert [row["source"] for row in client.get("/api/collectors").json()] == ["zoom", "slack_work", "gmail_personal", "domains"]

        # At rest: one ciphertext per connection, none of it readable.
        with db.get_session() as session:
            stored = session.exec(select(db.Connection)).all()
        assert len(stored) == 3
        assert all(row.secret_ciphertext and row.secret_ciphertext.startswith("gAAAAA") for row in stored)

    world.capture.assert_clean(files=True)


# --- 2: create, test, activate, collect -----------------------------------------------------------


def test_ui_created_connection_is_inactive_tested_activated_and_collected(world):
    world.enable_key()
    with world.app() as client:
        created = create_slack(client, "e2e", SLACK_OLD_TOKEN)
        assert created.status_code == 201
        body = created.json()
        assert set(body) == CONNECTION_FIELDS
        assert (body["id"], body["origin"], body["active"], body["health"]) == ("slack_e2e", "ui", False, None)
        assert body["secrets_set"] == ["token"]
        assert body["config"] == {"label": "e2e"}
        assert toggles(client)["slack_e2e"] is False

        tested = client.post("/api/connections/slack_e2e/test")
        assert tested.status_code == 200
        assert tested.json() == {"status": "ok", "detail": f"user=alice team=example, {len(health.SLACK_SCOPES)} scopes"}
        # The stored token, not .env, reached the health call. Testing does not switch it on.
        assert world.net.slack_tokens == [SLACK_OLD_TOKEN]
        assert rows_by_id(client)["slack_e2e"]["active"] is False

        activated = activate(client, "slack_e2e")
        assert activated.status_code == 200
        assert activated.json()["active_sources"]["slack_e2e"] is True
        assert world.rescheduled == [True]
        assert rows_by_id(client)["slack_e2e"]["active"] is True
        assert collector(client, "slack_e2e")["active"] is True
        assert collector(client, "slack_e2e")["last_attempt"] is None

        run_one(client, "slack_e2e")
        state = collector(client, "slack_e2e")
        attempt = state["last_attempt"]
        assert attempt["status"] == "ok"
        assert attempt["item_count"] == 2
        assert attempt["at"].endswith("Z")
        # The stub's detail quoted the token; the runner stored it scrubbed.
        assert MARKER in attempt["detail"]
        assert state["item_count"] == 2
        assert state["watermark"].endswith("Z")
        assert state["consecutive_failures"] == 0
        # The collector read the stored token from the overlay.
        assert world.net.dispatched == [("slack_e2e", SLACK_OLD_TOKEN)]
        assert world.net.slack_tokens == [SLACK_OLD_TOKEN, SLACK_OLD_TOKEN]

        page = client.get("/api/collectors/slack_e2e/items").json()
        assert page["total"] == 2
        assert sorted(item["preview"] for item in page["items"]) == ["hello from slack_e2e 1", "hello from slack_e2e 2"]
        detail = client.get(f"/api/collectors/slack_e2e/items/{page['items'][0]['id']}")
        assert detail.status_code == 200
        assert "hello from slack_e2e" in detail.json()["payload"]

        runs = client.get("/api/runs").json()
        assert (runs[0]["trigger"], runs[0]["status"]) == ("manual-source", "success")
        run_detail = client.get(f"/api/runs/{runs[0]['id']}").json()
        assert [(h["source"], h["status"]) for h in run_detail["source_health"]] == [("slack_e2e", "ok")]
        status = client.get("/api/status").json()
        assert status["last_run"]["status"] == "success"
        assert [(h["source"], h["status"]) for h in status["source_health"]] == [("slack_e2e", "ok")]
        assert rows_by_id(client)["slack_e2e"]["health"]["status"] == "ok"
        assert rows_by_id(client)["slack_e2e"]["health"]["checked_at"].endswith("Z")

    world.capture.assert_clean(files=True)


# --- 3: PATCH replaces only what it names ---------------------------------------------------------


def test_patch_replaces_only_the_named_secret_and_the_old_one_is_gone(world):
    world.enable_key()
    with world.app() as client:
        create_test_activate_and_run(client, SLACK_OLD_TOKEN)
        assert world.net.dispatched == [("slack_e2e", SLACK_OLD_TOKEN)]
        before = rows_by_id(client)["slack_e2e"]

        mark = len(world.capture.records)
        world.net.valid_tokens = {SLACK_NEW_TOKEN}
        patched = client.patch("/api/connections/slack_e2e", json={"secrets": {"token": SLACK_NEW_TOKEN}})
        assert patched.status_code == 200
        after = patched.json()
        assert set(after) == CONNECTION_FIELDS
        assert {key: after[key] for key in ("id", "kind", "label", "origin", "config", "secrets_set", "active")} == {
            key: before[key] for key in ("id", "kind", "label", "origin", "config", "secrets_set", "active")
        }

        # An edit form posts every box: a blank one means "leave as is", never "clear".
        blank = client.patch("/api/connections/slack_e2e", json={"secrets": {"token": ""}})
        assert blank.status_code == 200
        assert blank.json()["secrets_set"] == ["token"]

        # The new token is what every reader sees now; Slack would reject the old one.
        assert client.post("/api/connections/slack_e2e/test").json()["status"] == "ok"
        run_one(client, "slack_e2e")
        assert world.net.slack_tokens[-2:] == [SLACK_NEW_TOKEN, SLACK_NEW_TOKEN]
        assert world.net.dispatched[-1] == ("slack_e2e", SLACK_NEW_TOKEN)
        assert health.slack_workspaces()["e2e"] == SLACK_NEW_TOKEN
        assert SLACK_OLD_TOKEN not in connections.secret_values()
        assert SLACK_OLD_TOKEN not in health.env().values()

        # A seeded connection: a config-only PATCH keeps both secrets, and a one-secret PATCH keeps the other.
        config_only = client.patch("/api/connections/gmail_personal", json={"config": {"redirect_mode": "callback"}})
        assert config_only.status_code == 200
        assert config_only.json()["secrets_set"] == ["client_secret", "refresh_token"]
        refresh_only = client.patch("/api/connections/gmail_personal", json={"secrets": {"refresh_token": GMAIL_REFRESH_NEW}})
        assert refresh_only.status_code == 200
        assert refresh_only.json()["secrets_set"] == ["client_secret", "refresh_token"]
        assert refresh_only.json()["config"]["redirect_mode"] == "callback"
        assert connections.get_secret("gmail_personal", "client_secret") == GMAIL_SECRET
        assert connections.get_secret("gmail_personal", "refresh_token") == GMAIL_REFRESH_NEW

        for path in ("/api/connections", "/api/collectors", "/api/collectors/slack_e2e/items", "/api/status", "/api/system", "/api/runs", "/api/config"):
            assert client.get(path).status_code == 200

        # From the PATCH on, the old value is in no response, no stored text and no file.
        world.capture.assert_absent_since(mark, SLACK_OLD_TOKEN, "the pre-PATCH slack token")
        world.capture.assert_absent_since(mark, GMAIL_REFRESH_OLD, "the pre-PATCH gmail refresh token")
        assert SLACK_OLD_TOKEN.encode() not in (world.home / "test.db").read_bytes()
        assert collector(client, "slack_e2e")["last_attempt"]["status"] == "ok"

    world.capture.assert_clean(files=True)


# --- 4: tombstone and store-wins across restarts --------------------------------------------------


def test_delete_is_a_tombstone_and_the_store_wins_over_a_changed_env(world):
    world.enable_key()
    with world.app() as client:
        assert create_slack(client, "e2e", SLACK_OLD_TOKEN).status_code == 201
        deleted = client.delete("/api/connections/slack_work")
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert client.patch("/api/connections/zoom", json={"config": {"client_id": "zoomClientEdited"}}).status_code == 200
        edited = client.patch("/api/connections/zoom", json={"secrets": {"client_secret": ZOOM_SECRET_EDITED}})
        assert edited.status_code == 200
        assert client.patch("/api/connections/gmail_personal", json={"config": {"redirect_mode": "callback"}}).status_code == 200

        assert "slack_work" not in rows_by_id(client)
        assert "slack_work" not in toggles(client)
        assert store_state()["tombstones"] == ["slack_work"]

        # The operator edits .env while the app runs: the stale lines cannot bring anything back or override an edit.
        world.write_env(CHANGED_ENV_FILE)
        assert "slack_work" not in health.known_sources()
        assert health.env()["ZOOM_CLIENT_ID"] == "zoomClientEdited"

    world.restart()
    with world.app() as client:
        rows = rows_by_id(client)
        assert set(rows) == {"zoom", "gmail_personal", "slack_e2e"}
        assert "slack_work" not in health.known_sources()
        assert "slack_work" not in [row["source"] for row in client.get("/api/collectors").json()]
        assert "slack_work" not in toggles(client)
        assert client.post("/api/collectors/slack_work/run").status_code == 404

        # STORE WINS: the edit survives a restart even though .env now says something else.
        assert rows["zoom"]["config"] == {"account_id": "acct_E2eExample", "client_id": "zoomClientEdited"}
        assert rows["zoom"]["origin"] == "env"
        assert rows["gmail_personal"]["config"]["client_id"] == GMAIL_CLIENT_ID
        assert rows["gmail_personal"]["config"]["redirect_mode"] == "callback"
        assert connections.get_secret("zoom", "client_secret") == ZOOM_SECRET_EDITED
        live = health.env()
        assert live["ZOOM_CLIENT_ID"] == "zoomClientEdited"
        assert live["ZOOM_ACCOUNT_ID"] == "acct_E2eExample"
        assert live["ZOOM_CLIENT_SECRET"] == ZOOM_SECRET_EDITED
        assert "SLACK_WORK_TOKEN" not in live
        # A connection created in the UI is not something .env ever declared, and it survives too.
        assert rows["slack_e2e"]["origin"] == "ui"
        assert rows["slack_e2e"]["secrets_set"] == ["token"]
        assert connections.get_secret("slack_e2e", "token") == SLACK_OLD_TOKEN
        assert store_state()["tombstones"] == ["slack_work"]

    world.capture.assert_clean(files=True)


# --- 5: sweep of a whole session ------------------------------------------------------------------


def error_probes(client: Recorder) -> None:
    """Every kind of failure, each one carrying a credential in the place a person would paste it."""
    label = client.post("/api/connections", json={"kind": "slack", "label": "BAD LABEL", "token": PASTED_VALUE})
    assert label.status_code == 422
    assert label.json()["detail"][0]["loc"] == ["body", "label"]

    kind = client.post("/api/connections", json={"kind": PASTED_VALUE, "label": "x", "token": PASTED_VALUE})
    assert kind.status_code == 422

    unknown = client.post(
        "/api/connections", json={"kind": "slack", "label": "probe", "token": PASTED_VALUE, "oops": PASTED_VALUE}
    )
    assert unknown.status_code == 422

    duplicate = create_slack(client, "e2e", PASTED_VALUE)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "duplicate_connection"

    key_name = client.patch("/api/connections/slack_e2e", json={"secrets": {PASTED_KEY: "some-value"}})
    assert key_name.status_code == 422

    assert client.patch("/api/connections/slack_nope", json={"secrets": {"token": PASTED_VALUE}}).status_code == 404
    relabel = client.patch("/api/connections/slack_e2e", json={"config": {"label": "other"}, "secrets": {"token": PASTED_VALUE}})
    assert relabel.status_code == 422

    assert client.post(f"/api/connections/{PASTED_PATH}/test").status_code == 404
    missing = client.post(f"/api/collectors/{PASTED_PATH}/run")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "unknown_source"

    assert client.put("/api/config", json={"schedule_cron": PASTED_VALUE}).status_code == 422
    assert client.post("/api/collectors/slack_e2e/dry-run", json={"hours": 0, "note": PASTED_VALUE}).status_code == 422

    assert client.post("/api/connections", json={"kind": "slack", "label": "x", "token": PASTED_VALUE}, headers={"X-Requested-With": ""}).status_code == 403
    text_body = client.post(
        "/api/connections", content=f"token={PASTED_VALUE}", headers={"Content-Type": "text/plain", **WRITE}
    )
    assert text_body.status_code == 415


def test_sweep_no_response_of_a_whole_session_carries_a_secret_or_a_ciphertext(world):
    world.enable_key()
    with world.app() as client:
        assert len(rows_by_id(client)) == 3
        create_test_activate_and_run(client, SLACK_OLD_TOKEN)
        error_probes(client)

        dry = client.post("/api/collectors/slack_e2e/dry-run", json={"hours": 24, "limit": 5})
        assert dry.status_code == 202
        job = poll_dry_run(client, dry.json()["job_id"])
        assert job["status"] == "done"
        assert job["result"]["count"] == 2

        world.net.valid_tokens = {SLACK_NEW_TOKEN, GMAIL_REFRESH_NEW}
        assert client.patch("/api/connections/slack_e2e", json={"secrets": {"token": SLACK_NEW_TOKEN}}).status_code == 200
        assert client.patch("/api/connections/zoom", json={"secrets": {"client_secret": ZOOM_SECRET_EDITED}}).status_code == 200
        assert client.patch("/api/connections/gmail_personal", json={"secrets": {"refresh_token": GMAIL_REFRESH_NEW}}).status_code == 200
        assert client.post("/api/connections/slack_e2e/test").json()["status"] == "ok"
        run_one(client, "slack_e2e")
        run_one(client, "slack_work")
        assert client.delete("/api/connections/slack_work").status_code == 204

        assert client.post("/api/collectors/slack_e2e/reset").status_code == 200
        assert client.post("/api/collectors/slack_e2e/clear-failures").status_code == 200

        run_ids = [run["id"] for run in client.get("/api/runs").json()]
        assert len(run_ids) == 3
        for path in (
            "/api/connections",
            "/api/collectors",
            "/api/collectors/slack_e2e/items",
            "/api/status",
            "/api/system",
            "/api/runs",
            "/api/config",
            "/api/health",
            *[f"/api/runs/{run_id}" for run_id in run_ids],
        ):
            assert client.get(path).status_code == 200, path
        item_id = client.get("/api/collectors/slack_e2e/items").json()["items"][0]["id"]
        assert client.get(f"/api/collectors/slack_e2e/items/{item_id}").status_code == 200

    world.restart()
    with world.app() as client:
        for path in ("/api/connections", "/api/collectors", "/api/status", "/api/system", "/api/runs"):
            assert client.get(path).status_code == 200, path

    capture = world.capture
    urls = {record.url for record in capture.records}
    assert {"/api/connections", "/api/status", "/api/system", "/api/runs", "/api/config", "/api/collectors"} <= urls
    assert any(url.startswith("/api/runs/") for url in urls)
    assert {200, 201, 202, 204, 403, 404, 409, 415, 422} <= {record.status for record in capture.records}
    # A sweep over nothing proves nothing: several ciphertexts existed and were searched for.
    assert len(capture.records) >= 45
    capture.snapshot_ciphertexts()
    assert len(capture.ciphertexts) >= 5
    # Every credential the session used really reached the app, so absence is not vacuous.
    assert {SLACK_OLD_TOKEN, SLACK_NEW_TOKEN} <= set(world.net.slack_tokens)
    capture.assert_clean(files=True)


# --- 6: CSRF --------------------------------------------------------------------------------------


MUTATIONS: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", "/api/connections", {"json": {"kind": "slack", "label": "evil", "token": PASTED_VALUE}}),
    ("PATCH", "/api/connections/zoom", {"json": {"config": {"client_id": "hijacked"}}}),
    ("PATCH", "/api/connections/slack_work", {"json": {"secrets": {"token": PASTED_VALUE}}}),
    ("DELETE", "/api/connections/slack_work", {}),
    ("POST", "/api/connections/slack_work/test", {}),
    ("PUT", "/api/config", {"json": {"schedule_cron": "0 1 * * *", "tracker": "none", "active_sources": {"slack_work": False}}}),
    ("POST", "/api/collectors/slack_work/run", {}),
    ("POST", "/api/collectors/slack_work/reset", {}),
    ("POST", "/api/collectors/slack_work/clear-failures", {}),
    ("POST", "/api/collectors/slack_work/dry-run", {"json": {}}),
    ("POST", "/api/runs/trigger", {}),
]


def test_csrf_writes_without_the_header_or_from_a_foreign_origin_are_rejected_and_change_nothing(world):
    world.enable_key()
    with world.app(headers={}) as client:
        assert client.get("/api/config").status_code == 200
        before = store_state()
        listing_before = client.get("/api/connections").json()

        attempts = {
            "no header": {},
            "wrong header value": {"X-Requested-With": "XMLHttpRequest"},
            "foreign origin": {**WRITE, "Origin": FOREIGN_ORIGIN},
            "foreign origin, no header": {"Origin": FOREIGN_ORIGIN},
            "opaque origin": {**WRITE, "Origin": "null"},
        }
        for name, headers in attempts.items():
            for method, url, kwargs in MUTATIONS:
                resp = client.request(method, url, headers=headers, **kwargs)
                assert resp.status_code == 403, f"{name}: {method} {url} -> {resp.status_code}"

        # The body a form post would send is refused on its own, header or not.
        form = client.post("/api/connections", content=f"token={PASTED_VALUE}", headers={**WRITE, "Content-Type": "text/plain"})
        assert form.status_code == 415
        urlencoded = client.post(
            "/api/connections", data={"kind": "slack", "label": "evil", "token": PASTED_VALUE}, headers=WRITE
        )
        assert urlencoded.status_code == 415

        assert store_state() == before
        assert client.get("/api/connections").json() == listing_before
        assert world.net.slack_tokens == []
        assert world.net.dispatched == []
        assert world.rescheduled == []
        assert collectors_router._jobs == {}

        # A foreign origin gets no CORS grant, so a browser could not read an answer either.
        preflight = client.options(
            "/api/connections",
            headers={"Origin": FOREIGN_ORIGIN, "Access-Control-Request-Method": "DELETE", "Access-Control-Request-Headers": "x-requested-with"},
        )
        assert "access-control-allow-origin" not in preflight.headers
        assert "access-control-allow-credentials" not in preflight.headers

        # Controls: reads need no header, and the same write is accepted with the header from the allowed origin.
        assert client.get("/api/connections", headers={"Origin": FOREIGN_ORIGIN}).status_code == 200
        ok = client.post("/api/connections/slack_work/test", headers={**WRITE, "Origin": ALLOWED_ORIGIN})
        assert ok.status_code == 200
        assert ok.json()["status"] == "ok"
        assert ok.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert world.net.slack_tokens == [SLACK_WORK_TOKEN]
        changed = client.patch(
            "/api/connections/zoom", json={"config": {"client_id": "zoomClientEdited"}}, headers={**WRITE, "Origin": ALLOWED_ORIGIN}
        )
        assert changed.status_code == 200
        assert store_state() != before

    world.capture.assert_clean(files=True)


# --- 7: no key ------------------------------------------------------------------------------------


def test_without_a_key_the_store_is_inactive_secret_writes_get_503_and_env_collection_works(world):
    with world.app() as client:
        system = client.get("/api/system").json()
        assert system["secret_key_configured"] is False
        assert system["store_active"] is False
        assert connections.get_vault() is None
        assert client.get("/api/connections").json() == []

        writes = [
            {"kind": "slack", "label": "e2e", "token": SLACK_OLD_TOKEN},
            {"kind": "zoom", "account_id": "acct_Other", "client_id": "zoomOther", "client_secret": ZOOM_SECRET_EDITED},
            {"kind": "gmail", "label": "other", "client_id": GMAIL_CLIENT_ID, "client_secret": GMAIL_SECRET, "refresh_token": GMAIL_REFRESH_NEW},
        ]
        for payload in writes:
            resp = client.post("/api/connections", json=payload)
            assert resp.status_code == 503, payload["kind"]
            assert resp.json()["detail"]["code"] == "secret_key_missing"
        # Without secrets it is refused as well, and rolled back completely.
        m365 = client.post("/api/connections", json={"kind": "m365", "alias": "work", "tenant_id": "tenant-e2e.example.com", "client_id": "m365-client-e2e"})
        assert m365.status_code == 503
        assert m365.json()["detail"]["code"] == "store_inactive"
        assert client.patch("/api/connections/slack_work", json={"secrets": {"token": SLACK_NEW_TOKEN}}).status_code == 404

        state = store_state()
        assert state["connections"] == []
        assert state["tombstones"] == []
        assert "slack_e2e" not in toggles(client)

        # .env alone still drives collection, exactly as before the store existed.
        assert {"zoom", "slack_work", "gmail_personal"} <= set(toggles(client))
        run_one(client, "slack_work")
        assert world.net.slack_tokens == [SLACK_WORK_TOKEN]
        assert world.net.dispatched == [("slack_work", SLACK_WORK_TOKEN)]
        state = collector(client, "slack_work")
        assert state["last_attempt"]["status"] == "ok"
        assert state["last_attempt"]["item_count"] == 2
        assert state["item_count"] == 2
        assert client.get("/api/collectors/slack_work/items").json()["total"] == 2
        runs = client.get("/api/runs").json()
        assert (runs[0]["trigger"], runs[0]["status"]) == ("manual-source", "success")
        assert client.get("/api/status").json()["last_run"]["status"] == "success"
        assert client.get("/api/connections").json() == []
        assert store_state()["connections"] == []

    world.capture.assert_clean(files=True)


# --- 8: Zoom OAuth ----------------------------------------------------------------------------


def zoom_isolated(world: World) -> None:
    """
    health.check_zoom's refresh path uses a module-level cross-process lock file and an in-process
    token cache computed once at import time from the real data/ dir (T-005/T-010, see
    pipeline.health.ZOOM_REFRESH_LOCK and _zoom_cache); world's db.DB_PATH patch does not retarget
    either, so every test that calls the Zoom test endpoint must isolate them itself.
    """
    world.monkeypatch.setattr(health, "ZOOM_REFRESH_LOCK", world.home / "zoom_refresh.lock")
    world.monkeypatch.setattr(health, "_zoom_cache", None)


def test_zoom_oauth_create_start_and_callback_sign_in_store_only_the_refresh_token(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.enable_key()
    zoom_isolated(world)
    world.write_env(ZOOM_OAUTH_ENV_FILE)
    world.net.zoom = ZoomTokenStub()
    with world.app(base_url="https://testserver") as client:
        assert client.get("/api/system").json()["oauth"]["zoom"]["modes"] == ["callback"]

        created = create_zoom_oauth(client)
        assert set(created) == CONNECTION_FIELDS
        assert (created["id"], created["origin"], created["active"]) == ("zoom", "ui", False)
        assert created["config"] == {
            "client_id": ZOOM_OAUTH_CLIENT_ID,
            "auth_mode": "oauth",
            "include_transcripts": "true",
        }
        assert created["secrets_set"] == ["client_secret"]
        assert toggles(client)["zoom"] is False

        paste_refused = client.post("/api/oauth/zoom/paste", json={"flow_id": "whatever", "url": "http://x/?state=s&code=c"})
        assert paste_refused.status_code in (400, 404)  # a real flow_id is not needed to prove paste is refused

        paste_back = client.post("/api/oauth/zoom/start", json={"connection_id": "zoom", "mode": "paste_back"})
        assert paste_back.status_code == 400
        assert paste_back.json()["detail"]["code"] == "invalid_mode"

        sign_in_zoom(client)

        assert len(world.net.zoom.exchange_calls) == 1
        exchange = world.net.zoom.exchange_calls[0]
        assert exchange["data"]["code"] == ZOOM_AUTH_CODE
        assert exchange["data"]["redirect_uri"] == f"{ZOOM_PUBLIC_BASE_URL}/api/oauth/callback/zoom"
        assert exchange["auth"] == (ZOOM_OAUTH_CLIENT_ID, ZOOM_OAUTH_CLIENT_SECRET)

        assert connections.get_secret("zoom", "refresh_token") == ZOOM_REFRESH_INITIAL
        after = rows_by_id(client)["zoom"]
        assert after["secrets_set"] == ["client_secret", "refresh_token"]
        assert after["active"] is False  # signing in does not switch it on

    world.capture.assert_clean(files=True)
    assert_no_secrets_in_logs(caplog, world.capture.needles())


def test_zoom_oauth_test_button_rotates_and_persists_the_refresh_token(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.enable_key()
    zoom_isolated(world)
    world.write_env(ZOOM_OAUTH_ENV_FILE)
    world.net.zoom = ZoomTokenStub()
    with world.app(base_url="https://testserver") as client:
        create_zoom_oauth(client)
        sign_in_zoom(client)
        assert connections.get_secret("zoom", "refresh_token") == ZOOM_REFRESH_INITIAL

        tested = client.post("/api/connections/zoom/test")
        assert tested.status_code == 200
        result = tested.json()
        assert result["status"] == "ok"
        assert "signed in" in result["detail"]

        assert len(world.net.zoom.refresh_calls) == 1
        refreshed = world.net.zoom.refresh_calls[0]
        assert refreshed["data"]["refresh_token"] == ZOOM_REFRESH_INITIAL
        assert refreshed["auth"] == (ZOOM_OAUTH_CLIENT_ID, ZOOM_OAUTH_CLIENT_SECRET)
        assert connections.get_secret("zoom", "refresh_token") == ZOOM_REFRESH_ROTATED

        still_tested = client.post("/api/connections/zoom/test")
        assert still_tested.status_code == 200

    world.capture.assert_clean(files=True)
    assert_no_secrets_in_logs(caplog, world.capture.needles())


def test_zoom_oauth_missing_scope_on_test_names_the_missing_scope(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.enable_key()
    zoom_isolated(world)
    world.write_env(ZOOM_OAUTH_ENV_FILE)
    world.net.zoom = ZoomTokenStub(granted_scope=" ".join(sorted(health.ZOOM_USER_SCOPES)))  # no transcript scope
    with world.app(base_url="https://testserver") as client:
        create_zoom_oauth(client)
        sign_in_zoom(client)

        tested = client.post("/api/connections/zoom/test")
        assert tested.status_code == 200
        result = tested.json()
        assert result["status"] == "error"
        assert health.ZOOM_TRANSCRIPT_SCOPE in result["detail"]

    world.capture.assert_clean(files=True)
    assert_no_secrets_in_logs(caplog, world.capture.needles())


def test_zoom_oauth_test_button_invalid_grant_reports_sign_in_again(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.enable_key()
    zoom_isolated(world)
    world.write_env(ZOOM_OAUTH_ENV_FILE)
    world.net.zoom = ZoomTokenStub()
    with world.app(base_url="https://testserver") as client:
        create_zoom_oauth(client)
        sign_in_zoom(client)

        world.net.zoom.refresh_status = 400
        world.net.zoom.refresh_error = "invalid_grant"
        tested = client.post("/api/connections/zoom/test")

        assert tested.status_code == 200
        result = tested.json()
        assert result["status"] == "error"
        assert "sign in again" in result["detail"]
        # A rejected refresh cannot be persisted (Zoom rotates on use; nothing to rotate to here).
        assert connections.get_secret("zoom", "refresh_token") == ZOOM_REFRESH_INITIAL

    world.capture.assert_clean(files=True)
    assert_no_secrets_in_logs(caplog, world.capture.needles())


def test_zoom_oauth_env_s2s_seed_keeps_its_effective_auth_mode_and_id(world, caplog):
    """Unchanged behaviour: a .env with ZOOM_ACCOUNT_ID/ZOOM_CLIENT_ID/ZOOM_CLIENT_SECRET (the
    default ENV_FILE, written by the world fixture) seeds connection id "zoom", origin "env", with
    no browser sign-in on offer and an effective auth_mode of s2s even though config never says so."""
    caplog.set_level(logging.DEBUG)
    world.enable_key()
    zoom_isolated(world)
    with world.app() as client:
        assert client.get("/api/system").json()["oauth"]["zoom"]["modes"] == []  # no PUBLIC_BASE_URL

        rows = rows_by_id(client)
        assert rows["zoom"]["origin"] == "env"
        assert rows["zoom"]["config"] == {"account_id": "acct_E2eExample", "client_id": "zoomClientE2e"}
        assert "auth_mode" not in rows["zoom"]["config"]
        zoom_view = connections.get("zoom")
        assert zoom_view is not None
        assert connections.zoom_auth_mode(zoom_view) == "s2s"

        start_refused = client.post("/api/oauth/zoom/start", json={"connection_id": "zoom", "mode": "callback"})
        assert start_refused.status_code == 400  # no PUBLIC_BASE_URL: callback_unavailable

    world.capture.assert_clean(files=True)
    assert_no_secrets_in_logs(caplog, world.capture.needles())
