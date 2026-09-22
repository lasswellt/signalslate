"""
Startup wiring of the real app (api.main): routers registered, .env seeded once, the env overlay
registered, /system and a redacted /status.

Every test runs the REAL lifespan (TestClient used as a context manager) against a temp database,
a temp ROOT holding a temp .env, and a scheduler whose start is replaced by a no-op (a true
external: it would spawn a thread and read the clock). The vault and the overlay are process-global,
so the fixture resets both afterwards and no test leaves either behind.
"""
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as api_main  # noqa: E402
from pipeline import config_store, connections, crypto, db, health  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}

SLACK_TOKEN = "xoxb-startup-test-Hd91XvB7mR"
ZOOM_SECRET = "zoom-startup-secret-Qw83nLk2Pz"
ENV_FILE = (
    f"SLACK_WORK_TOKEN={SLACK_TOKEN}\n"
    "ZOOM_ACCOUNT_ID=acct_Example123\n"
    "ZOOM_CLIENT_ID=zoomClient_Ex\n"
    f"ZOOM_CLIENT_SECRET={ZOOM_SECRET}\n"
)


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Temp DB, temp ROOT (so .env is ours), temp config file, no-op scheduler; store state reset after."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(api_main, "start_scheduler", lambda: None)
    (tmp_path / ".env").write_text(ENV_FILE)
    yield tmp_path
    connections.set_vault(None)
    health.set_env_overlay_provider(None, None)


@contextmanager
def running_app() -> Iterator[TestClient]:
    with TestClient(api_main.app, headers=WRITE) as client:
        yield client


def _with_key(monkeypatch) -> str:
    key = crypto.generate_key()
    monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", key)
    return key


def _ids(client: TestClient) -> dict[str, dict]:
    resp = client.get("/api/connections")
    assert resp.status_code == 200
    return {row["id"]: row for row in resp.json()}


def _add_run(summary: Optional[str], error: Optional[str], detail: Optional[str]) -> None:
    with db.get_session() as session:
        run = db.Run(trigger="manual-source", status="failed", summary=summary, error=error)
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None
        session.add(db.SourceHealth(run_id=run.id, source="slack_work", status="error", detail=detail))
        session.commit()


def test_no_key_store_inactive_and_env_only_behaviour_unchanged(home, capsys):
    with running_app() as client:
        system = client.get("/api/system").json()
        assert system["secret_key_configured"] is False
        assert system["store_active"] is False
        assert connections.get_vault() is None
        # .env still drives everything, exactly as before the store existed.
        assert "slack_work" in health.known_sources()
        assert "zoom" in health.known_sources()
        assert client.get("/api/connections").json() == []
        assert [row["source"] for row in client.get("/api/collectors").json()] == ["zoom", "slack_work"]
    assert "connection store active" not in capsys.readouterr().out


def test_key_seeds_env_connections_and_overlay_serves_them(home, monkeypatch, capsys):
    key = _with_key(monkeypatch)
    with running_app() as client:
        system = client.get("/api/system").json()
        assert system["secret_key_configured"] is True
        assert system["store_active"] is True

        rows = _ids(client)
        assert set(rows) == {"zoom", "slack_work"}
        assert {row["origin"] for row in rows.values()} == {"env"}

        # The store, not .env, is now the source of truth: empty the file and both stay declared.
        (home / ".env").write_text("")
        assert set(health.known_sources()) == {"zoom", "slack_work"}
        assert "slack_work" in {row["source"] for row in client.get("/api/collectors").json()}

    out = capsys.readouterr().out
    assert "seeded 2 from .env, 2 in store" in out
    for secret in (key, SLACK_TOKEN, ZOOM_SECRET):
        assert secret not in out


def test_ui_created_connection_reaches_known_sources_through_the_overlay(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app() as client:
        resp = client.post("/api/connections", json={"kind": "slack", "label": "extra", "token": "xoxb-ui-Kd83LmQp2Zr"})
        assert resp.status_code == 201
        assert "slack_extra" in health.known_sources()


def test_second_startup_neither_reseeds_a_tombstone_nor_overwrites_an_edit(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app() as client:
        assert client.delete("/api/connections/slack_work").status_code == 204
        edited = client.patch("/api/connections/zoom", json={"config": {"client_id": "zoomClient_Edited"}})
        assert edited.status_code == 200

    # A whole new startup on the same database, with the same stale .env still declaring both.
    with running_app() as client:
        rows = _ids(client)
        assert "slack_work" not in rows
        assert rows["zoom"]["config"]["client_id"] == "zoomClient_Edited"
        assert "slack_work" not in health.known_sources()


def test_invalid_key_starts_env_only_and_never_prints_it(home, monkeypatch, capsys):
    bad_key = "not-a-fernet-key-Zq8Lm2"
    monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", bad_key)
    with running_app() as client:
        assert client.get("/api/health").json() == {"ok": True}
        system = client.get("/api/system").json()
        assert system["secret_key_configured"] is True
        assert system["store_active"] is False
        assert connections.get_vault() is None
        assert "slack_work" in health.known_sources()
        assert client.get("/api/connections").json() == []

    out = capsys.readouterr().out
    assert "not usable" in out
    assert bad_key not in out


def test_startup_state_does_not_leak_from_a_keyed_app_into_a_keyless_one(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app() as client:
        assert client.get("/api/system").json()["store_active"] is True
    monkeypatch.delenv("SIGNALSLATE_SECRET_KEY")
    with running_app() as client:
        assert client.get("/api/system").json()["store_active"] is False


def test_system_reports_callback_only_for_an_https_public_url(home, monkeypatch):
    with running_app() as client:
        plain = client.get("/api/system").json()
        assert plain["public_base_url_configured"] is False
        assert plain["oauth"] == {
            "google": {"modes": ["paste_back"]},
            "microsoft": {"modes": ["paste_back"]},
            "zoom": {"modes": []},
        }
        assert plain["web_origins"] == ["http://localhost:3000"]

        monkeypatch.setenv("PUBLIC_BASE_URL", "http://insecure.example.com")
        assert client.get("/api/system").json()["public_base_url_configured"] is False

        monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.com")
        monkeypatch.setenv("WEB_ORIGINS", "https://app.example.com, https://ui.example.org")
        https = client.get("/api/system")
        body = https.json()
        assert body["public_base_url_configured"] is True
        assert body["oauth"]["google"]["modes"] == ["paste_back", "callback"]
        assert body["oauth"]["microsoft"]["modes"] == ["paste_back", "callback"]
        assert body["oauth"]["zoom"]["modes"] == ["callback"]
        assert body["web_origins"] == ["https://app.example.com", "https://ui.example.org"]
        # The URL itself is config, not something the UI needs echoed.
        assert "insecure.example.com" not in https.text


def test_system_never_echoes_the_key(home, monkeypatch):
    key = _with_key(monkeypatch)
    with running_app() as client:
        assert key not in client.get("/api/system").text


def test_status_text_is_redacted_and_timestamps_carry_z(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app() as client:
        # The first is a stored secret with no recognisable shape (scrubbed by value); the second
        # is token-shaped and was never stored (scrubbed by shape).
        shapeless = ZOOM_SECRET
        shaped = "xoxb-999999999999-never-stored-Aa1Bb2Cc3"
        _add_run(
            summary=f"finished, last error quoted {shapeless}",
            error=f"HTTP 401 for token {shaped}",
            detail=f"auth failed with {shapeless} and {shaped}",
        )
        resp = client.get("/api/status")
        assert resp.status_code == 200
        for secret in (shapeless, shaped):
            assert secret not in resp.text
        body = resp.json()
        assert "[redacted]" in body["last_run"]["error"]
        assert "[redacted]" in body["source_health"][0]["detail"]
        assert body["last_run"]["trigger"] == "manual-source"
        assert body["last_run"]["started_at"].endswith("Z")
        assert body["source_health"][0]["checked_at"].endswith("Z")


def test_status_without_runs_is_still_well_formed(home):
    with running_app() as client:
        body = client.get("/api/status").json()
        assert body["last_run"] is None
        assert body["source_health"] == []
        assert body["next_scheduled_run"] is None


def test_new_routers_are_registered_under_api(home):
    paths = {getattr(route, "path", None) for route in api_main.app.routes}
    assert {
        "/api/connections",
        "/api/connections/{connection_id}",
        "/api/collectors",
        "/api/collectors/{source}/items",
        "/api/oauth/{provider}/start",
        "/api/oauth/{provider}/paste",
        "/api/oauth/callback/{provider}",
        "/api/system",
    } <= paths


def test_dry_run_poll_and_items_routes_both_resolve_through_the_real_app(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app() as client:
        # Handled by /collectors/dry-run/{job_id}, not swallowed as source "dry-run".
        poll = client.get("/api/collectors/dry-run/does-not-exist")
        assert poll.status_code == 404
        assert poll.json()["detail"]["code"] == "dry_run_not_found"

        # Handled by /collectors/{source}/items for a real source.
        items = client.get("/api/collectors/slack_work/items")
        assert items.status_code == 200
        assert items.json() == {"items": [], "next_before_id": None, "total": 0}

        missing = client.get("/api/collectors/nope/items")
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "unknown_source"


def test_oauth_router_answers_through_the_real_app(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app() as client:
        # No such connection: proves the route is wired and the guard/handlers are the real ones.
        resp = client.post("/api/oauth/google/start", json={"connection_id": "gmail_nobody", "mode": "paste_back"})
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "connection_not_found"
        # A write without the custom header is refused by the app-wide guard.
        bare = TestClient(api_main.app)
        assert bare.post("/api/oauth/google/start", json={"connection_id": "x", "mode": "paste_back"}).status_code == 403


def test_helper_leaves_no_state_between_tests(home):
    assert connections.get_vault() is None
    assert health.known_sources() == ["zoom", "slack_work"]
