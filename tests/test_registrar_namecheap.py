"""
Tests for pipeline.domains.registrars.namecheap.NamecheapClient.

No network: requests is stubbed at the module's own import site (module.requests.get), following
tests/test_collectors.py:47's route() pattern. Connections come from a real temp-DB + vault-backed
pipeline.connections store, mirroring tests/test_connections.py's fixtures, so from_connection-style
construction is exercised for real rather than mocked.
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
    IpNotWhitelisted,
    NotEligible,
    Quote,
    RateLimited,
    RegistrarError,
)
from pipeline.domains.registrars import namecheap  # noqa: E402

NAMECHEAP_KEY = "namecheap-key-Xy82QpL4vM"
NAMECHEAP_CONFIG = {
    "label": "prod",
    "api_user": "nc_user",
    "username": "nc_user",
    "client_ip": "203.0.113.5",
    "api_key": NAMECHEAP_KEY,
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

_RESPONSE_NS = 'xmlns="http://api.namecheap.com/xml.response"'


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
    view = connections.create("namecheap", NAMECHEAP_CONFIG)
    return view.id


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"status {self.status_code}")


def route(monkeypatch, handler):
    """Stubs namecheap.requests.get with handler(url, params) -> FakeResponse."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params or {}})
        return handler(url, params or {})

    monkeypatch.setattr(namecheap.requests, "get", fake_get)
    return calls


def _ok(body: str) -> FakeResponse:
    return FakeResponse(
        f'<?xml version="1.0" encoding="utf-8"?><ApiResponse {_RESPONSE_NS} Status="OK">{body}</ApiResponse>'.encode()
    )


def _error(message: str) -> FakeResponse:
    return FakeResponse(
        (
            f'<?xml version="1.0" encoding="utf-8"?><ApiResponse {_RESPONSE_NS} Status="ERROR">'
            f'<Errors><Error Number="9999">{message}</Error></Errors></ApiResponse>'
        ).encode()
    )


def _router(handler):
    def dispatch(url, params):
        return handler(params.get("Command"), params)

    return dispatch


# --- config / construction ----------------------------------------------------------


def test_from_connection_reads_config_and_secret(connection_id):
    client = namecheap.NamecheapClient(connection_id)
    assert client.kind == "namecheap"
    assert client._config.api_user == "nc_user"
    assert client._config.api_key == NAMECHEAP_KEY
    assert client._config.client_ip == "203.0.113.5"
    assert client._config.sandbox is False


def test_from_connection_unknown_id_raises_auth_failed(vault):
    with pytest.raises(AuthFailed):
        namecheap.NamecheapClient("namecheap_nonexistent")


def test_from_connection_no_api_key_raises_auth_failed(vault):
    fields = {k: v for k, v in NAMECHEAP_CONFIG.items() if k != "api_key"}
    with pytest.raises(connections.MissingField):
        connections.create("namecheap", fields)


def test_sandbox_true_uses_sandbox_host(monkeypatch, vault):
    view = connections.create("namecheap", {**NAMECHEAP_CONFIG, "sandbox": "true"})
    client = namecheap.NamecheapClient(view.id)
    assert client._host == namecheap._SANDBOX_HOST
    assert "api.sandbox.namecheap.com" in client._host


# --- list_domains --------------------------------------------------------------------


def test_list_domains_single_page(monkeypatch, connection_id):
    def handler(command, params):
        assert command == namecheap._CMD_GET_LIST
        assert params["PageSize"] == "100"
        return _ok(
            '<CommandResponse Type="namecheap.domains.getList"><DomainGetListResult>'
            '<Domain Name="Example.com" Expires="09/21/2027" IsLocked="true" AutoRenew="false" WhoisGuard="ENABLED"/>'
            '</DomainGetListResult></CommandResponse>'
        )

    route(monkeypatch, _router(handler))
    client = namecheap.NamecheapClient(connection_id)
    domains = client.list_domains()
    assert len(domains) == 1
    d = domains[0]
    assert d.name == "example.com"
    assert d.expires_at is not None
    assert d.expires_at.isoformat() == "2027-09-21T00:00:00"
    assert d.locked is True
    assert d.auto_renew is False
    assert d.privacy is True
    assert d.nameservers == ()


def test_list_domains_pages_until_short_page(monkeypatch, connection_id):
    page_one = "".join(f'<Domain Name="d{i}.com"/>' for i in range(namecheap._PAGE_SIZE))
    page_two = '<Domain Name="last.com"/>'
    calls = {"n": 0}

    def handler(command, params):
        calls["n"] += 1
        body = page_one if params["Page"] == "1" else page_two
        return _ok(f'<CommandResponse><DomainGetListResult>{body}</DomainGetListResult></CommandResponse>')

    route(monkeypatch, _router(handler))
    client = namecheap.NamecheapClient(connection_id)
    domains = client.list_domains()
    assert calls["n"] == 2
    assert len(domains) == namecheap._PAGE_SIZE + 1
    assert domains[-1].name == "last.com"


# --- check ------------------------------------------------------------------------


def test_check_chunks_at_batch_size(monkeypatch, connection_id):
    names = [f"name{i}.com" for i in range(namecheap._CHECK_BATCH_SIZE + 5)]
    seen_chunks = []

    def handler(command, params):
        assert command == namecheap._CMD_CHECK
        chunk = params["DomainList"].split(",")
        seen_chunks.append(chunk)
        entries = "".join(f'<DomainCheckResult Domain="{n}" Available="true" IsPremiumName="false"/>' for n in chunk)
        return _ok(f"<CommandResponse>{entries}</CommandResponse>")

    route(monkeypatch, _router(handler))
    client = namecheap.NamecheapClient(connection_id)
    quotes = client.check(names)
    assert len(seen_chunks) == 2
    assert len(seen_chunks[0]) == namecheap._CHECK_BATCH_SIZE
    assert len(seen_chunks[1]) == 5
    assert len(quotes) == len(names)
    assert all(isinstance(q, Quote) and q.available for q in quotes)


def test_check_premium_quote_has_price(monkeypatch, connection_id):
    def handler(command, params):
        return _ok(
            '<CommandResponse><DomainCheckResult Domain="premium.com" Available="true" '
            'IsPremiumName="true" PremiumRegistrationPrice="199.00" PremiumRenewalPrice="99.00"/></CommandResponse>'
        )

    route(monkeypatch, _router(handler))
    client = namecheap.NamecheapClient(connection_id)
    [quote] = client.check(["premium.com"])
    assert quote.premium is True
    assert quote.price == Decimal("199.00")
    assert quote.renewal_price == Decimal("99.00")
    assert quote.currency == namecheap._ACCOUNT_CURRENCY


# --- purchase -----------------------------------------------------------------------


def test_purchase_sends_all_four_contact_roles(monkeypatch, connection_id):
    captured = {}

    def handler(command, params):
        assert command == namecheap._CMD_CREATE
        captured.update(params)
        return _ok('<CommandResponse><DomainCreateResult Domain="newdomain.com" Registered="true" ChargedAmount="12.98" OrderID="555"/></CommandResponse>')

    route(monkeypatch, _router(handler))
    client = namecheap.NamecheapClient(connection_id)
    quote = Quote(name="newdomain.com", available=True, premium=False, price=Decimal("12.98"), renewal_price=None, currency="USD")
    result = client.purchase(quote, years=1, contact=CONTACT)

    assert result.success is True
    assert result.order_id == "555"
    assert result.charged == Decimal("12.98")
    for role in namecheap._CONTACT_ROLES:
        assert captured[f"{role}FirstName"] == "Jane"
        assert captured[f"{role}EmailAddress"] == "jane@example.com"


def test_purchase_registered_false_is_not_success(monkeypatch, connection_id):
    def handler(command, params):
        return _ok('<CommandResponse><DomainCreateResult Domain="newdomain.com" Registered="false"/></CommandResponse>')

    route(monkeypatch, _router(handler))
    client = namecheap.NamecheapClient(connection_id)
    quote = Quote(name="newdomain.com", available=True, premium=False, price=Decimal("0"), renewal_price=None, currency="USD")
    result = client.purchase(quote, years=1, contact=CONTACT)
    assert result.success is False
    assert result.charged is None


# --- error mapping --------------------------------------------------------------------


@pytest.mark.parametrize(
    "message,exc_type",
    [
        ("IP address not whitelisted for this account", IpNotWhitelisted),
        ("Access denied - IP not registered", IpNotWhitelisted),
        ("Your account is not eligible for this API", NotEligible),
        ("API access is not enabled for this account", NotEligible),
        ("Too many requests, rate limit exceeded", RateLimited),
        ("API Key is invalid", AuthFailed),
        ("Something else entirely broke", RegistrarError),
    ],
)
def test_error_messages_map_to_typed_errors(monkeypatch, connection_id, message, exc_type):
    def handler(command, params):
        return _error(message)

    route(monkeypatch, _router(handler))
    client = namecheap.NamecheapClient(connection_id)
    with pytest.raises(exc_type):
        client.list_domains()


def test_error_message_never_leaks_into_exception(monkeypatch, connection_id):
    secret_like = "ApiKey_Xy82QpL4vM_should_not_leak"

    def handler(command, params):
        return _error(f"API Key is invalid: {secret_like}")

    route(monkeypatch, _router(handler))
    client = namecheap.NamecheapClient(connection_id)
    with pytest.raises(AuthFailed) as info:
        client.list_domains()
    assert secret_like not in str(info.value)


# --- XXE ------------------------------------------------------------------------------


def test_xxe_payload_is_rejected_not_resolved(monkeypatch, connection_id):
    xxe = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE ApiResponse [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        f'<ApiResponse {_RESPONSE_NS} Status="OK"><CommandResponse>&xxe;</CommandResponse></ApiResponse>'
    ).encode()

    def handler(command, params):
        return FakeResponse(xxe)

    route(monkeypatch, _router(handler))
    client = namecheap.NamecheapClient(connection_id)
    with pytest.raises(RegistrarError) as info:
        client.list_domains()
    # Must fail closed with a generic message, never resolve the entity or echo /etc/passwd content.
    assert "root:" not in str(info.value)


# --- check_connection ------------------------------------------------------------------


def test_check_connection_success_reports_egress_ip(monkeypatch, connection_id):
    def dispatch(url, params):
        if "ipify" in url:
            return FakeResponse(b"203.0.113.9")
        assert params.get("Command") == namecheap._CMD_GET_BALANCES
        return _ok('<CommandResponse><UserGetBalancesResult Currency="USD" AvailableBalance="42.00"/></CommandResponse>')

    route(monkeypatch, dispatch)
    client = namecheap.NamecheapClient(connection_id)
    detail = client.check_connection()
    assert "203.0.113.9" in detail
    assert "42.00" in detail
    assert "USD" in detail


def test_check_connection_ip_not_whitelisted_names_egress_ip(monkeypatch, connection_id):
    def dispatch(url, params):
        if "ipify" in url:
            return FakeResponse(b"203.0.113.9")
        return _error("IP address not whitelisted for this account")

    route(monkeypatch, dispatch)
    client = namecheap.NamecheapClient(connection_id)
    with pytest.raises(IpNotWhitelisted) as info:
        client.check_connection()
    assert "203.0.113.9" in str(info.value)


def test_check_connection_ipify_failure_falls_back_to_unknown(monkeypatch, connection_id):
    def dispatch(url, params):
        if "ipify" in url:
            raise namecheap.requests.RequestException("boom")
        return _ok('<CommandResponse><UserGetBalancesResult Currency="USD" AvailableBalance="1.00"/></CommandResponse>')

    route(monkeypatch, dispatch)
    client = namecheap.NamecheapClient(connection_id)
    detail = client.check_connection()
    assert "unknown" in detail
