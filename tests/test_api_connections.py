"""
Tests for api.routers.connections. The property under test above all others: a submitted secret
and its ciphertext never appear in ANY response, error body or log line. Every test that touches a
secret sweeps what it received for every secret used in this file, and for every stored ciphertext.

Router-only app with install_security (the real guard and non-echoing 422 handler), never entered
as a context manager so the real lifespan does not run. The env overlay is registered the way
startup will register it, because the create endpoint depends on it.
"""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import connections as connections_router  # noqa: E402
from api.security import install_security  # noqa: E402
from api.serialize import iso_z  # noqa: E402
from pipeline import config_store, connections, crypto, db, health  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}

ZOOM_SECRET = "zoom-secret-Qw83nLk2Pz"
SLACK_TOKEN = "slack-token-Hd91XvB7mR"
GMAIL_SECRET = "gmail-client-secret-Tj40cUe5Ya"
GMAIL_REFRESH = "gmail-refresh-Vn27sGf1Lo"
NEW_SLACK_TOKEN = "slack-token-Rotated-Bk55Lq0Wz"
PASTED = "pasted-credential-Zx19Mn64Ty"
ALL_SECRETS = [ZOOM_SECRET, SLACK_TOKEN, GMAIL_SECRET, GMAIL_REFRESH, NEW_SLACK_TOKEN, PASTED]

M365 = {"kind": "m365", "alias": "Contoso-1", "tenant_id": "11111111-2222-3333-4444-555555555555", "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
ZOOM = {"kind": "zoom", "account_id": "acct_Example123", "client_id": "zoomClient_Ex", "client_secret": ZOOM_SECRET}
SLACK = {"kind": "slack", "label": "work", "token": SLACK_TOKEN}
GMAIL = {
    "kind": "gmail",
    "label": "personal",
    "client_id": "1234-example.apps.googleusercontent.com",
    "client_secret": GMAIL_SECRET,
    "refresh_token": GMAIL_REFRESH,
}


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def env(monkeypatch, tmp_path, temp_db):
    """A vault, an empty .env, a temp config file and the overlay registered like startup does."""
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    health.set_env_overlay_provider(connections.overlay_provider, connections.is_family_key)
    yield tmp_path
    connections.set_vault(None)


@pytest.fixture
def client(env) -> TestClient:
    app = FastAPI()
    install_security(app, allowed_origins=["https://ui.example.com"])
    app.include_router(connections_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


@pytest.fixture
def no_key_client(monkeypatch, tmp_path, temp_db) -> TestClient:
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    connections.set_vault(None)
    health.set_env_overlay_provider(connections.overlay_provider, connections.is_family_key)
    app = FastAPI()
    install_security(app, allowed_origins=["https://ui.example.com"])
    app.include_router(connections_router.router, prefix="/api")
    return TestClient(app, headers=WRITE)


def _ciphertexts() -> list[str]:
    with db.get_session() as session:
        return [row.secret_ciphertext for row in session.exec(select(db.Connection)).all() if row.secret_ciphertext]


def assert_clean(*responses: Any) -> None:
    """No secret used anywhere in this file, and no stored ciphertext, in any of the responses."""
    haystack = "\n".join(r.text + json.dumps(dict(r.headers)) for r in responses)
    for secret in ALL_SECRETS:
        assert secret not in haystack
    for ciphertext in _ciphertexts():
        assert ciphertext not in haystack
    assert "secret_ciphertext" not in haystack


def _source_health(source: str, status: str, detail: str | None, checked_at: datetime) -> None:
    with db.get_session() as session:
        run = db.Run(trigger="manual", status="success")
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None
        session.add(db.SourceHealth(run_id=run.id, source=source, status=status, detail=detail, checked_at=checked_at))
        session.commit()


def _toggles() -> dict[str, bool]:
    return json.loads(config_store.CONFIG_PATH.read_text())["active_sources"]


# --- iso_z -----------------------------------------------------------------------------------


def test_iso_z_marks_naive_utc_and_passes_none_through():
    assert iso_z(datetime(2026, 9, 21, 10, 5, 3, 999999)) == "2026-09-21T10:05:03Z"
    assert iso_z(None) is None


def test_iso_z_converts_an_aware_datetime_to_utc():
    from datetime import timedelta, timezone

    aware = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert iso_z(aware) == "2026-09-21T10:00:00Z"


# --- create ----------------------------------------------------------------------------------


def test_create_returns_201_inactive_and_write_only(client):
    resp = client.post("/api/connections", json=SLACK)

    assert resp.status_code == 201
    body = resp.json()
    assert body == {
        "id": "slack_work",
        "kind": "slack",
        "label": "work",
        "origin": "ui",
        "config": {"label": "work"},
        "secrets_set": ["token"],
        "active": False,
        "health": None,
    }
    assert _toggles()["slack_work"] is False
    assert_clean(resp)


def test_create_of_every_kind_is_inactive_and_write_only(client):
    responses = [client.post("/api/connections", json=payload) for payload in (M365, ZOOM, SLACK, GMAIL)]

    assert [r.status_code for r in responses] == [201] * 4
    assert [r.json()["id"] for r in responses] == ["m365_Contoso-1", "zoom", "slack_work", "gmail_personal"]
    assert responses[3].json()["secrets_set"] == ["client_secret", "refresh_token"]
    assert responses[3].json()["config"]["redirect_mode"] == "paste_back"
    toggles = _toggles()
    assert [toggles[i] for i in ("m365_Contoso-1", "zoom", "slack_work", "gmail_personal")] == [False] * 4
    assert_clean(*responses)


def test_created_connection_is_listed_inactive_and_the_scheduler_view_agrees(client):
    client.post("/api/connections", json=SLACK)

    assert config_store.load_config()["active_sources"]["slack_work"] is False
    listed = client.get("/api/connections").json()
    assert [c["active"] for c in listed] == [False]


def test_duplicate_create_is_409_and_write_only(client):
    first = client.post("/api/connections", json=SLACK)
    second = client.post("/api/connections", json={**SLACK, "token": NEW_SLACK_TOKEN})

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "duplicate_connection"
    assert_clean(first, second)
    assert connections.get_secret("slack_work", "token") == SLACK_TOKEN


def test_create_with_secrets_and_no_key_is_503_secret_key_missing(no_key_client):
    resp = no_key_client.post("/api/connections", json=SLACK)

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "secret_key_missing"
    assert_clean(resp)
    assert connections.list_connections() == []


def test_create_that_the_overlay_cannot_see_is_rolled_back_with_store_inactive(no_key_client):
    """No key means no overlay, so an m365 connection (no secrets) is not in known_sources()."""
    resp = no_key_client.post("/api/connections", json=M365)

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "store_inactive"
    assert connections.list_connections() == []
    with db.get_session() as session:
        assert session.exec(select(db.Tombstone)).all() == []


def test_rollback_restores_a_tombstone_that_existed_before_the_create(no_key_client):
    """Create clears a tombstone; a rolled-back create must put it back so .env cannot re-seed."""
    with db.get_session() as session:
        session.add(db.Tombstone(id="m365_Contoso-1", deleted_at=datetime(2026, 9, 1)))
        session.commit()

    resp = no_key_client.post("/api/connections", json=M365)

    assert resp.status_code == 503
    with db.get_session() as session:
        assert [t.id for t in session.exec(select(db.Tombstone)).all()] == ["m365_Contoso-1"]


# --- 422: no echo ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {**SLACK, "label": "Has Space", "token": PASTED},
        {**SLACK, "token": PASTED, "unexpected": "x"},
        {**SLACK, "token": PASTED, PASTED: "x"},
        {**SLACK, "token": 12345, "label": PASTED},
        {"kind": PASTED, "token": PASTED},
        {"token": PASTED},
        {**ZOOM, "client_secret": "   "},
        {**ZOOM, "client_id": PASTED + "!"},
        {**GMAIL, "redirect_mode": PASTED, "client_secret": PASTED},
        {**GMAIL, "refresh_token": "bad\x00" + PASTED},
    ],
)
def test_422_carries_field_names_and_never_a_submitted_value(client, payload):
    resp = client.post("/api/connections", json=payload)

    assert resp.status_code == 422
    assert PASTED not in resp.text
    assert_clean(resp)
    detail = resp.json()["detail"]
    assert detail and all(set(item) == {"type", "loc", "msg"} for item in detail)


def test_422_names_the_invalid_field(client):
    resp = client.post("/api/connections", json={**SLACK, "label": "Has Space"})

    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {"type": "value_error", "loc": ["body", "label"], "msg": resp.json()["detail"][0]["msg"]}
    ]
    assert connections.list_connections() == []


def test_a_write_without_the_required_header_is_rejected_before_the_handler(client):
    resp = client.post("/api/connections", json=SLACK, headers={"X-Requested-With": ""})

    assert resp.status_code == 403
    assert connections.list_connections() == []
    assert_clean(resp)


# --- list ------------------------------------------------------------------------------------


def test_list_is_write_only_and_health_is_null_until_a_check_ran(client):
    for payload in (M365, ZOOM, SLACK, GMAIL):
        client.post("/api/connections", json=payload)

    resp = client.get("/api/connections")

    assert resp.status_code == 200
    body = resp.json()
    assert [c["id"] for c in body] == ["m365_Contoso-1", "zoom", "slack_work", "gmail_personal"]
    assert all(c["health"] is None for c in body)
    assert all(set(c) == {"id", "kind", "label", "origin", "config", "secrets_set", "active", "health"} for c in body)
    assert_clean(resp)


def test_list_health_is_the_latest_row_with_a_z_timestamp(client):
    client.post("/api/connections", json=SLACK)
    _source_health("slack_work", "error", "old failure", datetime(2026, 9, 20, 6, 0, 0))
    _source_health("slack_work", "ok", "user=alice team=example", datetime(2026, 9, 21, 6, 30, 15))
    _source_health("zoom", "ok", "belongs to another connection", datetime(2026, 9, 21, 7, 0, 0))

    body = client.get("/api/connections").json()

    assert body[0]["health"] == {"status": "ok", "detail": "user=alice team=example", "checked_at": "2026-09-21T06:30:15Z"}


def test_list_redacts_a_stored_secret_inside_a_health_detail(client):
    """The token has no recognizable shape, so only the by-value pass of redact() can catch it."""
    client.post("/api/connections", json=SLACK)
    _source_health(
        "slack_work",
        "error",
        f"401 from upstream, request echoed: {SLACK_TOKEN} for team",
        datetime(2026, 9, 21, 6, 0, 0),
    )

    resp = client.get("/api/connections")

    assert SLACK_TOKEN not in resp.text
    assert "[redacted]" in resp.json()[0]["health"]["detail"]
    assert_clean(resp)


def test_list_reports_the_names_of_secrets_set_even_when_the_stored_key_no_longer_opens_them(client):
    client.post("/api/connections", json=SLACK)
    connections.set_vault(crypto.Vault([crypto.generate_key()]))

    resp = client.get("/api/connections")

    assert resp.status_code == 200
    assert resp.json()[0]["secrets_set"] == []
    assert_clean(resp)


# --- patch -----------------------------------------------------------------------------------


def test_patch_replaces_only_the_secrets_provided(client):
    client.post("/api/connections", json=GMAIL)

    resp = client.patch("/api/connections/gmail_personal", json={"secrets": {"refresh_token": NEW_SLACK_TOKEN}})

    assert resp.status_code == 200
    assert resp.json()["secrets_set"] == ["client_secret", "refresh_token"]
    assert connections.get_secret("gmail_personal", "refresh_token") == NEW_SLACK_TOKEN
    assert connections.get_secret("gmail_personal", "client_secret") == GMAIL_SECRET
    assert_clean(resp)


def test_patch_with_an_empty_string_secret_leaves_it_as_is(client):
    client.post("/api/connections", json=GMAIL)

    resp = client.patch(
        "/api/connections/gmail_personal",
        json={"config": {"client_id": "5678-other.apps.googleusercontent.com"}, "secrets": {"client_secret": "", "refresh_token": ""}},
    )

    assert resp.status_code == 200
    assert resp.json()["config"]["client_id"] == "5678-other.apps.googleusercontent.com"
    assert resp.json()["secrets_set"] == ["client_secret", "refresh_token"]
    assert connections.get_secret("gmail_personal", "client_secret") == GMAIL_SECRET
    assert connections.get_secret("gmail_personal", "refresh_token") == GMAIL_REFRESH
    assert_clean(resp)


def test_patch_replaces_config_only(client):
    client.post("/api/connections", json=M365)

    resp = client.patch("/api/connections/m365_Contoso-1", json={"config": {"tenant_id": "contoso.example.com"}})

    assert resp.status_code == 200
    assert resp.json()["config"]["tenant_id"] == "contoso.example.com"
    assert resp.json()["active"] is False


def test_patch_keeps_the_toggle_and_reports_current_health(client):
    client.post("/api/connections", json=SLACK)
    config_store.set_source_active("slack_work", True)
    _source_health("slack_work", "ok", "fine", datetime(2026, 9, 21, 6, 0, 0))

    body = client.patch("/api/connections/slack_work", json={"secrets": {"token": NEW_SLACK_TOKEN}}).json()

    assert body["active"] is True
    assert body["health"]["status"] == "ok"


def test_patch_unknown_id_is_404(client):
    resp = client.patch("/api/connections/slack_nope", json={"secrets": {"token": NEW_SLACK_TOKEN}})

    assert resp.status_code == 404
    assert_clean(resp)


@pytest.mark.parametrize(
    "payload",
    [
        {"config": {"label": "other"}},
        {"config": {PASTED: "x"}},
        {"secrets": {PASTED: PASTED}},
        {"secrets": {"token": "  "}},
        {"config": {"label": 5}},
        {PASTED: "x"},
    ],
)
def test_patch_422_never_echoes_a_submitted_value(client, payload):
    client.post("/api/connections", json=SLACK)

    resp = client.patch("/api/connections/slack_work", json=payload)

    assert resp.status_code == 422
    assert PASTED not in resp.text
    assert_clean(resp)
    assert connections.get_secret("slack_work", "token") == SLACK_TOKEN


def test_patch_with_secrets_and_no_key_is_503_secret_key_missing(client):
    client.post("/api/connections", json=SLACK)
    connections.set_vault(None)

    resp = client.patch("/api/connections/slack_work", json={"secrets": {"token": NEW_SLACK_TOKEN}})

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "secret_key_missing"
    assert_clean(resp)


def test_patch_that_cannot_open_the_stored_envelope_is_503_and_write_only(client):
    client.post("/api/connections", json=SLACK)
    connections.set_vault(crypto.Vault([crypto.generate_key()]))

    resp = client.patch("/api/connections/slack_work", json={"secrets": {"token": NEW_SLACK_TOKEN}})

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "secret_decrypt_failed"
    assert_clean(resp)


# --- delete ----------------------------------------------------------------------------------


def test_delete_returns_204_and_removes_the_connection(client):
    client.post("/api/connections", json=SLACK)

    resp = client.delete("/api/connections/slack_work")

    assert resp.status_code == 204
    assert resp.content == b""
    assert client.get("/api/connections").json() == []


def test_delete_unknown_id_is_404(client):
    assert client.delete("/api/connections/slack_nope").status_code == 404


def test_delete_removes_the_toggle_and_the_watermark_but_keeps_items(client):
    client.post("/api/connections", json=SLACK)
    config_store.set_source_active("slack_work", True)
    db.set_cursor("slack_work", datetime(2026, 9, 20, 6, 0, 0))
    with db.get_session() as session:
        session.add(
            db.CollectedItem(
                run_id=_first_run_id(),
                source="slack_work",
                item_type="message",
                external_id="m-1",
                occurred_at=datetime(2026, 9, 20, 5, 0, 0),
                payload="{}",
            )
        )
        session.commit()

    client.delete("/api/connections/slack_work")

    assert "slack_work" not in json.loads(config_store.CONFIG_PATH.read_text())["active_sources"]
    with db.get_session() as session:
        assert session.get(db.SourceCursor, "slack_work") is None
        assert len(session.exec(select(db.CollectedItem)).all()) == 1


def _first_run_id() -> int:
    with db.get_session() as session:
        run = db.Run(trigger="manual", status="success")
        session.add(run)
        session.commit()
        session.refresh(run)
        assert run.id is not None
        return run.id


def test_recreating_a_deleted_connection_starts_inactive(client):
    client.post("/api/connections", json=SLACK)
    config_store.set_source_active("slack_work", True)
    client.delete("/api/connections/slack_work")

    resp = client.post("/api/connections", json=SLACK)

    assert resp.status_code == 201
    assert resp.json()["active"] is False
    assert _toggles()["slack_work"] is False


def test_delete_tombstones_so_env_seeding_cannot_bring_it_back(client):
    client.post("/api/connections", json=SLACK)

    client.delete("/api/connections/slack_work")

    assert connections.seed_from_env({"SLACK_WORK_TOKEN": SLACK_TOKEN}) == []
    assert client.get("/api/connections").json() == []


def test_an_env_seeded_connection_is_deletable_and_stays_deleted(client):
    assert connections.seed_from_env({"SLACK_WORK_TOKEN": SLACK_TOKEN}) == ["slack_work"]
    assert client.get("/api/connections").json()[0]["origin"] == "env"

    assert client.delete("/api/connections/slack_work").status_code == 204

    assert connections.seed_from_env({"SLACK_WORK_TOKEN": SLACK_TOKEN}) == []
    assert client.get("/api/connections").json() == []


# --- test endpoint ---------------------------------------------------------------------------


def test_test_endpoint_unknown_id_is_404(client):
    resp = client.post("/api/connections/slack_nope/test")

    assert resp.status_code == 404
    assert_clean(resp)


def test_test_endpoint_maps_an_ok_result_and_does_not_activate(client, monkeypatch):
    client.post("/api/connections", json=SLACK)
    seen: dict[str, Any] = {}

    def fake_slack(label, token):
        seen.update(label=label, token=token)
        return health.HealthResult("slack_work", "ok", "user=alice team=example, 9 scopes")

    monkeypatch.setattr(health, "check_slack", fake_slack)

    resp = client.post("/api/connections/slack_work/test")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "detail": "user=alice team=example, 9 scopes"}
    # The stored token reached the check through the overlay, and only there.
    assert seen == {"label": "work", "token": SLACK_TOKEN}
    assert _toggles()["slack_work"] is False
    assert_clean(resp)


def test_test_endpoint_maps_an_error_result_and_redacts_the_detail(client, monkeypatch):
    client.post("/api/connections", json=SLACK)
    monkeypatch.setattr(
        health,
        "check_slack",
        lambda label, token: health.HealthResult("slack_work", "error", f"invalid_auth for {token}"),
    )

    resp = client.post("/api/connections/slack_work/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "[redacted]" in body["detail"]
    assert_clean(resp)


def test_test_endpoint_dispatches_each_kind_to_its_check(client, monkeypatch):
    for payload in (M365, ZOOM, GMAIL):
        client.post("/api/connections", json=payload)
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(health, "check_m365", lambda alias: calls.append(("m365", alias)) or health.HealthResult("m365_" + alias, "ok", "m"))
    monkeypatch.setattr(health, "check_zoom", lambda: calls.append(("zoom", None)) or health.HealthResult("zoom", "ok", "z"))
    monkeypatch.setattr(health, "check_gmail", lambda label: calls.append(("gmail", label)) or health.HealthResult("gmail_" + label, "error", "g"))

    results = [client.post(f"/api/connections/{i}/test").json() for i in ("m365_Contoso-1", "zoom", "gmail_personal")]

    assert calls == [("m365", "Contoso-1"), ("zoom", None), ("gmail", "personal")]
    assert [r["status"] for r in results] == ["ok", "ok", "error"]


def test_test_endpoint_reports_a_raising_check_without_its_message(client, monkeypatch, caplog):
    client.post("/api/connections", json=SLACK)

    def boom(label, token):
        raise RuntimeError(f"connection reset while sending {token}")

    monkeypatch.setattr(health, "check_slack", boom)

    with caplog.at_level(logging.DEBUG):
        resp = client.post("/api/connections/slack_work/test")

    assert resp.status_code == 200
    assert resp.json() == {"status": "error", "detail": "The check could not be completed"}
    assert_clean(resp)
    assert SLACK_TOKEN not in caplog.text


def test_test_endpoint_with_a_real_check_and_a_missing_cache_reports_error_without_secrets(client):
    """No stub: check_m365 runs for real against an empty token directory and needs no network."""
    client.post("/api/connections", json=M365)

    resp = client.post("/api/connections/m365_Contoso-1/test")

    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert_clean(resp)


# --- the whole journey, swept ----------------------------------------------------------------


def test_no_secret_reaches_any_response_or_log_across_a_full_lifecycle(client, monkeypatch, caplog):
    monkeypatch.setattr(
        health, "check_zoom", lambda: health.HealthResult("zoom", "error", f"bad secret {ZOOM_SECRET}")
    )
    with caplog.at_level(logging.DEBUG):
        responses = [
            client.post("/api/connections", json=ZOOM),
            client.post("/api/connections", json=SLACK),
            client.post("/api/connections", json=GMAIL),
            client.post("/api/connections", json=SLACK),
            client.post("/api/connections", json={**SLACK, "label": "bad label", "token": PASTED}),
            client.get("/api/connections"),
            client.patch("/api/connections/slack_work", json={"secrets": {"token": NEW_SLACK_TOKEN}}),
            client.patch("/api/connections/slack_work", json={"secrets": {"token": PASTED, "bogus": PASTED}}),
            client.post("/api/connections/zoom/test"),
            client.get("/api/connections"),
            client.delete("/api/connections/gmail_personal"),
            client.get("/api/connections"),
        ]

    assert_clean(*responses)
    for secret in ALL_SECRETS:
        assert secret not in caplog.text
