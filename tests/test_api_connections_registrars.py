"""
Tests for api.routers.connections's registrar kinds (namecheap, godaddy, wordpress): create/update
request models and the /test dispatch that maps pipeline.domains.registrars typed errors to a
readable, secret-free detail.

Router-only app with install_security (the real guard and non-echoing 422 handler), a temp DB and
a vault, mirroring tests/test_api_connections.py. Every response is swept for api_key, api_secret,
registrant_contact and the stored ciphertext, same as that file's assert_clean.

The /test endpoint is exercised by monkeypatching connections_router.inventory.registrar_for (this
router module's own import site), not by mocking pipeline.domains.inventory/registrars themselves —
same pattern as tests/test_api_domains.py's domains_router.inventory.* monkeypatches. A real check
would need real network to Namecheap/GoDaddy/WordPress.com; the adapters have their own test files.
"""
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routers import connections as connections_router  # noqa: E402
from api.security import install_security  # noqa: E402
from pipeline import config_store, connections, crypto, db, health  # noqa: E402
from pipeline.domains.registrars import AuthFailed, IpNotWhitelisted, NotEligible, RateLimited, RegistrarError  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}

NAMECHEAP_KEY = "nc-api-key-Hq82VxTz01"
GODADDY_KEY = "gd-api-key-Bn47RfWm93"
GODADDY_SECRET = "gd-api-secret-Lp65TsCk28"
WORDPRESS_SECRET = "wp-client-secret-Yv13OeUj76"
WORDPRESS_TOKEN = "wp-access-token-Fz90MgHd44"
REGISTRANT_JSON = json.dumps(
    {
        "first_name": "Jane",
        "last_name": "Doe",
        "address1": "1 Main St",
        "city": "Springfield",
        "state_province": "IL",
        "postal_code": "62704",
        "country": "US",
        "phone": "+1.5551234567",
        "email": "jane@example.com",
    }
)
ALL_SECRETS = [NAMECHEAP_KEY, GODADDY_KEY, GODADDY_SECRET, WORDPRESS_SECRET, WORDPRESS_TOKEN, REGISTRANT_JSON]

NAMECHEAP = {
    "kind": "namecheap",
    "label": "primary",
    "api_user": "ncuser",
    "username": "ncuser",
    "client_ip": "203.0.113.5",
    "api_key": NAMECHEAP_KEY,
}
GODADDY = {
    "kind": "godaddy",
    "label": "primary",
    # auth_mode "classic": the legacy sso-key pair, exercised throughout this file. "pat" (a single
    # Personal Access Token, what developer.godaddy.com now issues) is the default and covered in
    # test_create_godaddy_pat_mode_defaults_and_works below.
    "auth_mode": "classic",
    "api_key": GODADDY_KEY,
    "api_secret": GODADDY_SECRET,
}
WORDPRESS = {
    "kind": "wordpress",
    "label": "primary",
    "client_id": "wp-client-123",
    "client_secret": WORDPRESS_SECRET,
}


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def env(monkeypatch, tmp_path, temp_db):
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


def _ciphertexts() -> list[str]:
    with db.get_session() as session:
        return [row.secret_ciphertext for row in session.exec(select(db.Connection)).all() if row.secret_ciphertext]


def assert_clean(*responses: Any) -> None:
    haystack = "\n".join(r.text + json.dumps(dict(r.headers)) for r in responses)
    for secret in ALL_SECRETS:
        assert secret not in haystack
    for ciphertext in _ciphertexts():
        assert ciphertext not in haystack
    assert "secret_ciphertext" not in haystack


class _FakeRegistrar:
    def __init__(self, fn: Callable[[], str]) -> None:
        self._fn = fn

    def check_connection(self) -> str:
        return self._fn()


def _stub_registrar(monkeypatch, fn: Callable[[], str]) -> None:
    monkeypatch.setattr(connections_router.inventory, "registrar_for", lambda connection_id: _FakeRegistrar(fn))


# --- create ----------------------------------------------------------------------------------


def test_create_of_every_registrar_kind_is_201_and_write_only(client):
    responses = [client.post("/api/connections", json=payload) for payload in (NAMECHEAP, GODADDY, WORDPRESS)]

    assert [r.status_code for r in responses] == [201, 201, 201]
    bodies = [r.json() for r in responses]
    assert [b["id"] for b in bodies] == ["namecheap_primary", "godaddy_primary", "wordpress_primary"]
    assert [b["kind"] for b in bodies] == ["namecheap", "godaddy", "wordpress"]
    assert bodies[0]["secrets_set"] == ["api_key"]
    assert bodies[1]["secrets_set"] == ["api_key", "api_secret"]
    assert bodies[2]["secrets_set"] == ["client_secret"]
    assert [b["active"] for b in bodies] == [False, False, False]
    assert bodies[0]["config"] == {"label": "primary", "api_user": "ncuser", "username": "ncuser", "client_ip": "203.0.113.5"}
    assert_clean(*responses)


def test_create_namecheap_with_registrant_contact_is_write_only(client):
    resp = client.post("/api/connections", json={**NAMECHEAP, "registrant_contact": REGISTRANT_JSON})

    assert resp.status_code == 201
    assert resp.json()["secrets_set"] == ["api_key", "registrant_contact"]
    assert_clean(resp)


def test_create_godaddy_with_registrant_contact_is_write_only(client):
    resp = client.post("/api/connections", json={**GODADDY, "registrant_contact": REGISTRANT_JSON})

    assert resp.status_code == 201
    assert resp.json()["secrets_set"] == ["api_key", "api_secret", "registrant_contact"]
    assert_clean(resp)


def test_create_godaddy_pat_mode_defaults_and_works(client):
    """auth_mode omitted defaults to "pat" (developer.godaddy.com's current signup); a single
    api_token is enough, and the classic api_key/api_secret pair must not be required."""
    resp = client.post(
        "/api/connections",
        json={"kind": "godaddy", "label": "patacct", "api_token": "gd-pat-Qw83RtNv56xLmE"},
    )

    assert resp.status_code == 201
    assert resp.json()["secrets_set"] == ["api_token"]
    assert_clean(resp)


def test_create_godaddy_pat_mode_without_token_is_422(client):
    resp = client.post("/api/connections", json={"kind": "godaddy", "label": "nopat"})

    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {"type": "value_error", "loc": ["body", "api_token"], "msg": resp.json()["detail"][0]["msg"]}
    ]
    assert_clean(resp)


def test_create_wordpress_with_access_token_is_write_only(client):
    resp = client.post("/api/connections", json={**WORDPRESS, "access_token": WORDPRESS_TOKEN})

    assert resp.status_code == 201
    assert resp.json()["secrets_set"] == ["access_token", "client_secret"]
    assert_clean(resp)


def test_registrar_create_does_not_hit_store_inactive(client):
    """Registrar kinds have no active_sources entry; the known_sources() gate must not apply to them."""
    resp = client.post("/api/connections", json=NAMECHEAP)

    assert resp.status_code == 201
    assert "namecheap_primary" not in config_store.load_config()["active_sources"]


def test_create_namecheap_invalid_client_ip_is_422_and_never_echoed(client):
    resp = client.post("/api/connections", json={**NAMECHEAP, "client_ip": "not-an-ip"})

    assert resp.status_code == 422
    assert resp.json()["detail"] == [
        {"type": "value_error", "loc": ["body", "client_ip"], "msg": resp.json()["detail"][0]["msg"]}
    ]
    assert_clean(resp)


def test_create_godaddy_unknown_field_is_422(client):
    resp = client.post("/api/connections", json={**GODADDY, "bogus": "x"})

    assert resp.status_code == 422
    assert_clean(resp)


# --- list --------------------------------------------------------------------------------------


def test_list_never_carries_a_registrar_secret(client):
    client.post("/api/connections", json={**NAMECHEAP, "registrant_contact": REGISTRANT_JSON})
    client.post("/api/connections", json={**GODADDY, "registrant_contact": REGISTRANT_JSON})
    client.post("/api/connections", json=WORDPRESS)

    resp = client.get("/api/connections")

    assert resp.status_code == 200
    assert len(resp.json()) == 3
    assert_clean(resp)


# --- patch -------------------------------------------------------------------------------------


def test_patch_replaces_only_the_secret_provided(client):
    client.post("/api/connections", json=NAMECHEAP)

    resp = client.patch("/api/connections/namecheap_primary", json={"secrets": {"api_key": "nc-rotated-Qx84LbFe17"}})

    assert resp.status_code == 200
    assert resp.json()["secrets_set"] == ["api_key"]
    assert connections.get_secret("namecheap_primary", "api_key") == "nc-rotated-Qx84LbFe17"
    assert_clean(resp)


# --- test endpoint: readable, secret-free mapping ---------------------------------------------


@pytest.mark.parametrize(
    "payload,connection_id",
    [(NAMECHEAP, "namecheap_primary"), (GODADDY, "godaddy_primary"), (WORDPRESS, "wordpress_primary")],
)
def test_test_endpoint_maps_an_ok_check_connection_result(client, monkeypatch, payload, connection_id):
    client.post("/api/connections", json=payload)
    _stub_registrar(monkeypatch, lambda: "connected; balance 12.34 USD")

    resp = client.post(f"/api/connections/{connection_id}/test")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "detail": "connected; balance 12.34 USD"}
    assert_clean(resp)


@pytest.mark.parametrize(
    "exc_cls,message",
    [
        (IpNotWhitelisted, "Namecheap rejected the request: caller IP is not whitelisted; whitelist 203.0.113.9 in Namecheap"),
        (NotEligible, "Namecheap API is not enabled for this account"),
        (AuthFailed, "Namecheap rejected the API credentials"),
        (RateLimited, "Namecheap rate limit exceeded"),
        (RegistrarError, "Namecheap API request failed"),
    ],
)
def test_test_endpoint_maps_each_registrar_error_to_a_readable_detail(client, monkeypatch, exc_cls, message):
    client.post("/api/connections", json=NAMECHEAP)

    def raise_it() -> str:
        raise exc_cls(message)

    _stub_registrar(monkeypatch, raise_it)

    resp = client.post("/api/connections/namecheap_primary/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["detail"] == message
    assert_clean(resp)


def test_test_endpoint_redacts_a_stored_secret_inside_the_check_connection_detail(client, monkeypatch):
    client.post("/api/connections", json=NAMECHEAP)
    _stub_registrar(monkeypatch, lambda: f"connected as {NAMECHEAP_KEY}")

    resp = client.post("/api/connections/namecheap_primary/test")

    assert resp.status_code == 200
    assert NAMECHEAP_KEY not in resp.text
    assert "[redacted]" in resp.json()["detail"]
    assert_clean(resp)


def test_test_endpoint_reports_an_unexpected_exception_without_its_message(client, monkeypatch, caplog):
    client.post("/api/connections", json=GODADDY)

    def raise_it() -> str:
        raise ValueError(f"unexpected, leaked secret {GODADDY_KEY}")

    _stub_registrar(monkeypatch, raise_it)

    resp = client.post("/api/connections/godaddy_primary/test")

    assert resp.status_code == 200
    assert resp.json() == {"status": "error", "detail": "The check could not be completed"}
    assert_clean(resp)
    assert GODADDY_KEY not in caplog.text


def test_test_endpoint_does_not_activate_a_registrar_connection(client, monkeypatch):
    client.post("/api/connections", json=WORDPRESS)
    _stub_registrar(monkeypatch, lambda: "ok")

    client.post("/api/connections/wordpress_primary/test")

    assert "wordpress_primary" not in config_store.load_config()["active_sources"]


# --- the whole journey, swept ------------------------------------------------------------------


def test_no_registrar_secret_reaches_any_response_or_log_across_a_full_lifecycle(client, monkeypatch, caplog):
    _stub_registrar(monkeypatch, lambda: f"balance rejected for {GODADDY_KEY}")

    with caplog.at_level(logging.DEBUG):
        responses = [
            client.post("/api/connections", json=NAMECHEAP),
            client.post("/api/connections", json={**GODADDY, "registrant_contact": REGISTRANT_JSON}),
            client.post("/api/connections", json=WORDPRESS),
            client.get("/api/connections"),
            client.patch("/api/connections/namecheap_primary", json={"secrets": {"api_key": "nc-rotated-Qx84LbFe17"}}),
            client.post("/api/connections/godaddy_primary/test"),
            client.get("/api/connections"),
            client.delete("/api/connections/wordpress_primary"),
        ]

    assert_clean(*responses)
    for secret in ALL_SECRETS:
        assert secret not in caplog.text
