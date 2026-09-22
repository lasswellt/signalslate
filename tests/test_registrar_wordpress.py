"""
Tests for pipeline.domains.registrars.wordpress.WordPressClient.

No network: requests is stubbed at the module's own import site (module.requests.get), following
tests/test_registrar_godaddy.py's route() pattern. Connections come from a real temp-DB +
vault-backed pipeline.connections store.
"""
import sys
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, crypto, db  # noqa: E402
from pipeline.domains.registrars import AuthFailed, RegistrarError, Unsupported  # noqa: E402
from pipeline.domains.registrars import wordpress  # noqa: E402

ACCESS_TOKEN = "wp-access-token-Xy82QpL4vM"
WORDPRESS_CONFIG = {
    "label": "main",
    "client_id": "12345",
    "client_secret": "wp-client-secret-9qL2pMvX",
}


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def vault(temp_db):
    v = crypto.Vault([crypto.generate_key()])
    connections.set_vault(v)
    yield v
    connections.set_vault(None)


@pytest.fixture
def connection_id(vault):
    view = connections.create("wordpress", {**WORDPRESS_CONFIG, "access_token": ACCESS_TOKEN})
    return view.id


@pytest.fixture
def signed_out_connection_id(vault):
    """A wordpress connection that was created but never completed sign-in (no access_token)."""
    view = connections.create("wordpress", WORDPRESS_CONFIG)
    return view.id


class FakeResponse:
    def __init__(self, json_body=None, status_code: int = 200):
        self._json_body = json_body
        self.status_code = status_code

    def json(self):
        if self._json_body is None:
            raise ValueError("no body")
        return self._json_body


def route(monkeypatch, handler):
    """Stubs wordpress.requests.get with handler(url, headers) -> FakeResponse."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}})
        return handler(url, headers or {})

    monkeypatch.setattr(wordpress.requests, "get", fake_get)
    return calls


# --- config / construction ----------------------------------------------------------


def test_from_connection_reads_access_token(connection_id):
    client = wordpress.WordPressClient(connection_id)
    assert client.kind == "wordpress"
    assert client._config.access_token == ACCESS_TOKEN


def test_from_connection_unknown_id_raises_auth_failed(vault):
    with pytest.raises(AuthFailed):
        wordpress.WordPressClient("wordpress_nonexistent")


def test_from_connection_no_access_token_raises_auth_failed(signed_out_connection_id):
    with pytest.raises(AuthFailed):
        wordpress.WordPressClient(signed_out_connection_id)


def test_requests_use_bearer_header(monkeypatch, connection_id):
    def handler(url, headers):
        return FakeResponse(json_body={"domains": []})

    calls = route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    client.list_domains()
    assert calls[0]["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert calls[0]["url"] == wordpress._ALL_DOMAINS_URL


# --- list_domains ---------------------------------------------------------------------


def test_list_domains_maps_known_fields(monkeypatch, connection_id):
    def handler(url, headers):
        return FakeResponse(json_body={
            "domains": [
                {"domain": "Example.com", "expiry": "2027-09-21T00:00:00Z", "auto_renewing": True},
            ]
        })

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    domains = client.list_domains()
    assert len(domains) == 1
    d = domains[0]
    assert d.name == "example.com"
    assert d.expires_at is not None
    assert d.expires_at.isoformat().startswith("2027-09-21T00:00:00")
    assert d.auto_renew is True
    assert d.locked is None
    assert d.privacy is None
    assert d.nameservers == ()


def test_list_domains_missing_optional_fields_map_to_none(monkeypatch, connection_id):
    def handler(url, headers):
        return FakeResponse(json_body={"domains": [{"domain": "bare.com"}]})

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    [d] = client.list_domains()
    assert d.name == "bare.com"
    assert d.expires_at is None
    assert d.auto_renew is None


def test_list_domains_skips_non_dict_entries(monkeypatch, connection_id):
    def handler(url, headers):
        return FakeResponse(json_body={"domains": [{"domain": "good.com"}, "not-a-dict", 42]})

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    domains = client.list_domains()
    assert len(domains) == 1
    assert domains[0].name == "good.com"


def test_list_domains_entry_without_name_is_skipped(monkeypatch, connection_id):
    def handler(url, headers):
        return FakeResponse(json_body={"domains": [{"expiry": "2027-01-01T00:00:00Z"}]})

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    assert client.list_domains() == []


@pytest.mark.parametrize("body", [
    [],
    "unexpected string",
    {"not_domains": []},
    {"domains": "not-a-list"},
    {"domains": None},
])
def test_list_domains_unknown_shape_raises_response_shape_changed(monkeypatch, connection_id, body):
    def handler(url, headers):
        return FakeResponse(json_body=body)

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    with pytest.raises(RegistrarError) as info:
        client.list_domains()
    assert "response shape changed" in str(info.value)


def test_list_domains_unparseable_json_raises_registrar_error(monkeypatch, connection_id):
    def handler(url, headers):
        return FakeResponse(json_body=None)

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    with pytest.raises(RegistrarError):
        client.list_domains()


# --- error mapping --------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_401_403_raise_auth_failed_sign_in_again(monkeypatch, connection_id, status):
    def handler(url, headers):
        return FakeResponse(json_body={"error": "authorization_required"}, status_code=status)

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    with pytest.raises(AuthFailed) as info:
        client.list_domains()
    assert "sign in again" in str(info.value)


def test_error_body_never_leaks_into_exception(monkeypatch, connection_id):
    secret_like = "access-token-should-not-leak"

    def handler(url, headers):
        return FakeResponse(json_body={"error": "authorization_required", "message": secret_like}, status_code=403)

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    with pytest.raises(AuthFailed) as info:
        client.list_domains()
    assert secret_like not in str(info.value)


def test_5xx_raises_generic_registrar_error(monkeypatch, connection_id):
    def handler(url, headers):
        return FakeResponse(json_body={"error": "server_error"}, status_code=500)

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    with pytest.raises(RegistrarError):
        client.list_domains()


def test_network_failure_raises_registrar_error(monkeypatch, connection_id):
    import requests as real_requests

    def fake_get(url, headers=None, timeout=None):
        raise real_requests.ConnectionError("boom")

    monkeypatch.setattr(wordpress.requests, "get", fake_get)
    client = wordpress.WordPressClient(connection_id)
    with pytest.raises(RegistrarError):
        client.list_domains()


# --- check / purchase: unsupported -----------------------------------------------------


def test_check_raises_unsupported(connection_id):
    client = wordpress.WordPressClient(connection_id)
    with pytest.raises(Unsupported):
        client.check(["example.com"])


def test_purchase_raises_unsupported(connection_id):
    from pipeline.domains.registrars import Contact, Quote
    from decimal import Decimal

    client = wordpress.WordPressClient(connection_id)
    quote = Quote(name="example.com", available=True, premium=False, price=Decimal("10"), renewal_price=None, currency="USD")
    contact = Contact(
        first_name="Jane", last_name="Doe", address1="123 Example St", city="Springfield",
        state_province="IL", postal_code="62704", country="US", phone="+1.2175551234", email="jane@example.com",
    )
    with pytest.raises(Unsupported):
        client.purchase(quote, years=1, contact=contact)


# --- check_connection ------------------------------------------------------------------


def test_check_connection_success(monkeypatch, connection_id):
    def handler(url, headers):
        return FakeResponse(json_body={"domains": []})

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    detail = client.check_connection()
    assert "wordpress" in detail.lower()


def test_check_connection_failure_raises_auth_failed(monkeypatch, connection_id):
    def handler(url, headers):
        return FakeResponse(json_body={"error": "authorization_required"}, status_code=403)

    route(monkeypatch, handler)
    client = wordpress.WordPressClient(connection_id)
    with pytest.raises(AuthFailed):
        client.check_connection()
