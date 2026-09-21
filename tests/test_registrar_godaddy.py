"""
Tests for pipeline.domains.registrars.godaddy.GoDaddyClient.

No network: requests is stubbed at the module's own import site (module.requests.request /
module.requests.get), following tests/test_collectors.py:47's route() pattern and
tests/test_registrar_namecheap.py's fixtures. Connections come from a real temp-DB + vault-backed
pipeline.connections store, so from_connection-style construction is exercised for real.
"""
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, crypto, db  # noqa: E402
from pipeline.domains.registrars import (  # noqa: E402
    AuthFailed,
    Contact,
    NotEligible,
    Quote,
    RateLimited,
    RegistrarError,
)
from pipeline.domains.registrars import godaddy  # noqa: E402

GODADDY_KEY = "godaddy-key-Xy82QpL4vM"
GODADDY_SECRET = "godaddy-secret-9qL2pMvX"
GODADDY_CONFIG = {
    "label": "prod",
    "api_key": GODADDY_KEY,
    "api_secret": GODADDY_SECRET,
}
CONTACT = Contact(
    first_name="Jane",
    last_name="Doe",
    address1="123 Example St",
    city="Springfield",
    state_province="IL",
    postal_code="62704",
    country="US",
    phone="+1.2175551234",
    email="jane@example.com",
)


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
    view = connections.create("godaddy", GODADDY_CONFIG)
    return view.id


class FakeResponse:
    def __init__(self, json_body=None, status_code: int = 200, headers=None, content: bytes = b"{}"):
        self._json_body = json_body
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content if json_body is None else b"1"

    def json(self):
        if self._json_body is None:
            raise ValueError("no body")
        return self._json_body


def route(monkeypatch, handler):
    """Stubs godaddy.requests.request with handler(method, url, params, json) -> FakeResponse."""
    calls = []

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        calls.append({"method": method, "url": url, "headers": headers or {}, "params": params or {}, "json": json})
        return handler(method, url, params or {}, json)

    monkeypatch.setattr(godaddy.requests, "request", fake_request)
    return calls


# --- config / construction ----------------------------------------------------------


def test_from_connection_reads_config_and_secrets(connection_id):
    client = godaddy.GoDaddyClient(connection_id)
    assert client.kind == "godaddy"
    assert client._config.api_key == GODADDY_KEY
    assert client._config.api_secret == GODADDY_SECRET
    assert client._config.environment == "production"


def test_from_connection_unknown_id_raises_auth_failed(vault):
    with pytest.raises(AuthFailed):
        godaddy.GoDaddyClient("godaddy_nonexistent")


def test_from_connection_no_secrets_raises_auth_failed(vault):
    fields = {k: v for k, v in GODADDY_CONFIG.items() if k not in ("api_key", "api_secret")}
    with pytest.raises(connections.MissingField):
        connections.create("godaddy", fields)


def test_ote_environment_uses_ote_host(vault):
    view = connections.create("godaddy", {**GODADDY_CONFIG, "environment": "ote"})
    client = godaddy.GoDaddyClient(view.id)
    assert client._host == godaddy._OTE_HOST
    assert "api.ote-godaddy.com" in client._host


def test_requests_use_sso_key_header(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body=[])

    calls = route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    client.list_domains()
    assert calls[0]["headers"]["Authorization"] == f"sso-key {GODADDY_KEY}:{GODADDY_SECRET}"


# --- list_domains ---------------------------------------------------------------------


def test_list_domains_single_page(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        assert method == "GET"
        assert params["limit"] == str(godaddy._LIST_LIMIT)
        return FakeResponse(json_body=[
            {
                "domain": "Example.com",
                "expires": "2027-09-21T00:00:00.000Z",
                "renewAuto": False,
                "locked": True,
                "nameServers": ["ns1.example.com", "ns2.example.com"],
            }
        ])

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    domains = client.list_domains()
    assert len(domains) == 1
    d = domains[0]
    assert d.name == "example.com"
    assert d.expires_at is not None
    assert d.expires_at.isoformat().startswith("2027-09-21T00:00:00")
    assert d.locked is True
    assert d.auto_renew is False
    assert d.privacy is None
    assert d.nameservers == ("ns1.example.com", "ns2.example.com")


def test_list_domains_pages_until_short_page(monkeypatch, connection_id):
    page_one = [{"domain": f"d{i}.com"} for i in range(godaddy._LIST_LIMIT)]
    page_two = [{"domain": "last.com"}]
    calls = {"n": 0}

    def handler(method, url, params, json_body):
        calls["n"] += 1
        if "marker" not in params:
            return FakeResponse(json_body=page_one)
        assert params["marker"] == f"d{godaddy._LIST_LIMIT - 1}.com"
        return FakeResponse(json_body=page_two)

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    domains = client.list_domains()
    assert calls["n"] == 2
    assert len(domains) == godaddy._LIST_LIMIT + 1
    assert domains[-1].name == "last.com"


# --- check ------------------------------------------------------------------------


def test_check_chunks_at_batch_size(monkeypatch, connection_id):
    names = [f"name{i}.com" for i in range(godaddy._CHECK_BATCH_SIZE + 5)]
    seen_chunks = []

    def handler(method, url, params, json_body):
        assert method == "POST"
        assert params["checkType"] == "FULL"
        seen_chunks.append(json_body)
        entries = [
            {"domain": n, "available": True, "premium": False, "price": 12990000, "currency": "USD"}
            for n in json_body
        ]
        return FakeResponse(json_body={"domains": entries})

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    quotes = client.check(names)
    assert len(seen_chunks) == 2
    assert len(seen_chunks[0]) == godaddy._CHECK_BATCH_SIZE
    assert len(seen_chunks[1]) == 5
    assert len(quotes) == len(names)
    assert all(isinstance(q, Quote) and q.available and q.price == Decimal("12.99") for q in quotes)


def test_check_unavailable_name_has_no_purchasable_price(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"domains": [{"domain": "taken.com", "available": False, "premium": False}]})

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    [quote] = client.check(["taken.com"])
    assert quote.available is False


def test_check_available_missing_price_raises_not_zero(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"domains": [{"domain": "obscure.xyz", "available": True, "premium": False}]})

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    with pytest.raises(RegistrarError) as info:
        client.check(["obscure.xyz"])
    assert "obscure.xyz" in str(info.value)


def test_check_premium_flag_and_price_micro_units(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"domains": [
            {"domain": "premium.com", "available": True, "premium": True, "price": 199000000, "currency": "USD"}
        ]})

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    [quote] = client.check(["premium.com"])
    assert quote.premium is True
    assert quote.price == Decimal("199")
    assert quote.currency == "USD"


# --- purchase -----------------------------------------------------------------------


def test_purchase_fetches_agreements_and_sends_consent(monkeypatch, connection_id):
    captured = {}

    def handler(method, url, params, json_body):
        if method == "GET" and url.endswith(godaddy._AGREEMENTS_PATH):
            assert params["tlds"] == "com"
            assert params["privacy"] == "false"
            return FakeResponse(json_body=[{"agreementKey": "DNRA"}, {"agreementKey": "REG"}])
        assert method == "POST" and url.endswith(godaddy._PURCHASE_PATH)
        captured.update(json_body)
        return FakeResponse(json_body={"orderId": "abc123"})

    route(monkeypatch, handler)

    def fake_get(url, params=None, timeout=None):
        class R:
            text = "203.0.113.9"

            def raise_for_status(self):
                pass
        return R()

    monkeypatch.setattr(godaddy.requests, "get", fake_get)
    client = godaddy.GoDaddyClient(connection_id)
    quote = Quote(name="newdomain.com", available=True, premium=False, price=Decimal("12.99"), renewal_price=None, currency="USD")
    result = client.purchase(quote, years=1, contact=CONTACT)

    assert result.success is True
    assert result.order_id == "abc123"
    assert result.charged == Decimal("12.99")
    assert captured["privacy"] is False
    assert captured["domain"] == "newdomain.com"
    assert captured["period"] == 1
    assert captured["consent"]["agreementKeys"] == ["DNRA", "REG"]
    assert captured["consent"]["agreedBy"] == "203.0.113.9"
    for role in ("contactAdmin", "contactBilling", "contactRegistrant", "contactTech"):
        assert captured[role]["nameFirst"] == "Jane"
        assert captured[role]["email"] == "jane@example.com"
        assert captured[role]["addressMailing"]["country"] == "US"


def test_purchase_no_agreements_raises(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        assert url.endswith(godaddy._AGREEMENTS_PATH)
        return FakeResponse(json_body=[])

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    quote = Quote(name="newdomain.com", available=True, premium=False, price=Decimal("12.99"), renewal_price=None, currency="USD")
    with pytest.raises(RegistrarError):
        client.purchase(quote, years=1, contact=CONTACT)


# --- error mapping --------------------------------------------------------------------


def test_401_raises_auth_failed(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"code": "INVALID_CREDENTIAL"}, status_code=401)

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    with pytest.raises(AuthFailed):
        client.list_domains()


@pytest.mark.parametrize("code", ["ACCESS_DENIED", "ACCOUNT_NOT_ELIGIBLE"])
def test_403_eligibility_codes_raise_not_eligible(monkeypatch, connection_id, code):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"code": code}, status_code=403)

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    with pytest.raises(NotEligible):
        client.check(["example.com"])


def test_403_other_code_raises_auth_failed(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"code": "FORBIDDEN"}, status_code=403)

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    with pytest.raises(AuthFailed):
        client.list_domains()


def test_429_raises_rate_limited_with_reset(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"code": "TOO_MANY_REQUESTS"}, status_code=429, headers={"RateLimit-Reset": "30"})

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    with pytest.raises(RateLimited) as info:
        client.list_domains()
    assert "30" in str(info.value)


def test_error_body_never_leaks_into_exception(monkeypatch, connection_id):
    secret_like = "api-secret-should-not-leak"

    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"code": "INVALID_CREDENTIAL", "message": secret_like}, status_code=401)

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    with pytest.raises(AuthFailed) as info:
        client.list_domains()
    assert secret_like not in str(info.value)


def test_5xx_raises_generic_registrar_error(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"code": "INTERNAL_ERROR"}, status_code=500)

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    with pytest.raises(RegistrarError):
        client.list_domains()


# --- check_connection ------------------------------------------------------------------


def test_check_connection_success(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        assert params.get("limit") == "1"
        return FakeResponse(json_body=[])

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    detail = client.check_connection()
    assert "godaddy" in detail.lower()
    assert "production" in detail


def test_check_connection_failure_raises_auth_failed(monkeypatch, connection_id):
    def handler(method, url, params, json_body):
        return FakeResponse(json_body={"code": "INVALID_CREDENTIAL"}, status_code=401)

    route(monkeypatch, handler)
    client = godaddy.GoDaddyClient(connection_id)
    with pytest.raises(AuthFailed):
        client.check_connection()
