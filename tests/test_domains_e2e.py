"""
End-to-end backend test for the domain collector (docs/plans/domain-collector/plan.md/spec.md).

Drives the real app (api.main.app via TestClient), mirroring tests/test_management_e2e.py's setup:
temp DB, a temp .env-backed config, a real Fernet vault, the real security middleware, every router.
Only true externals are replaced:

- namecheap.requests.get / godaddy.requests.request, the registrar HTTP clients' own import sites
  (tests/test_registrar_namecheap.py / test_registrar_godaddy.py's own route() pattern);
- the DNS resolver (dns.resolver.Resolver, via pipeline.domains.dns's own _make_resolver seam) and
  whoisit (via pipeline.domains.rdap.lookup_rdap's own keyword-default seam) that
  pipeline.domains.snapshot.inspect() calls with no injection from the API router — POST
  /api/domains/sync always exercises the real inspect()/lookup_records()/lookup_rdap() code, so
  these two true externals are faked at their own construction points instead.

No pipeline module is mocked, and pipeline.runner.dispatch/pipeline.collectors.dispatch are left
untouched: the "domains" collector (pipeline/collectors/domains.py) makes no network call of its
own, so running it for real needs no stub.
"""
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import dns.resolver
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, select
from whoisit.errors import ResourceDoesNotExist

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as api_main  # noqa: E402
from pipeline import config_store, connections, crypto, db, health  # noqa: E402
from pipeline.domains import dns as domain_dns  # noqa: E402
from pipeline.domains import rdap as domain_rdap  # noqa: E402
from pipeline.domains.registrars import godaddy, namecheap  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}

NAMECHEAP_API_KEY = "nc-e2e-SECRET-apikey-Qp82vXmL"
GODADDY_API_KEY = "gd-e2e-SECRET-apikey-Rt91wZnP"
GODADDY_API_SECRET = "gd-e2e-SECRET-apisecret-Ku73yBhQ"
REGISTRANT_CONTACT = {
    "first_name": "Jane",
    "last_name": "Doe",
    "address1": "123 Example St",
    "city": "Springfield",
    "state_province": "IL",
    "postal_code": "62704",
    "country": "US",
    "phone": "+1.2175551234",
    "email": "jane-e2e-SECRET-contact@example.com",
}
REGISTRANT_CONTACT_JSON = json.dumps(REGISTRANT_CONTACT)

# No secret is a substring of another, so a hit always names the value that leaked.
SECRETS: dict[str, str] = {
    "namecheap api key": NAMECHEAP_API_KEY,
    "godaddy api key": GODADDY_API_KEY,
    "godaddy api secret": GODADDY_API_SECRET,
    "registrant email": REGISTRANT_CONTACT["email"],
}


# --- recording --------------------------------------------------------------------------------


@dataclass
class Record:
    method: str
    url: str
    status: int
    blob: str


@dataclass
class Capture:
    records: list[Record] = field(default_factory=list)

    def record(self, method: str, url: str, response: httpx.Response) -> None:
        headers = "\n".join(f"{name}: {value}" for name, value in response.headers.multi_items())
        body = response.content.decode("utf-8", errors="replace")
        self.records.append(Record(method, url, response.status_code, f"{response.status_code}\n{headers}\n\n{body}"))

    def assert_clean(self) -> None:
        """Nothing secret in any recorded response (status line, headers and body)."""
        problems: list[str] = []
        for record in self.records:
            for label, value in SECRETS.items():
                if value in record.blob:
                    problems.append(f"{record.method} {record.url} ({record.status}) leaked {label}")
        assert not problems, problems

    def assert_items_clean(self) -> None:
        """Nothing secret in any stored CollectedItem payload."""
        with db.get_session() as session:
            payloads = [row.payload for row in session.exec(select(db.CollectedItem)).all()]
        problems: list[str] = []
        for payload in payloads:
            for label, value in SECRETS.items():
                if value in payload:
                    problems.append(f"collecteditem payload leaked {label}")
        assert not problems, problems


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


# --- fake externals -----------------------------------------------------------------------------


class FakeRdata:
    def __init__(self, text: str) -> None:
        self._text = text

    def to_text(self) -> str:
        return self._text


class FakeResolver:
    """records maps (qname, rdtype) -> list[str] (answer texts); a key absent from records is NXDOMAIN."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], list[str]] = {}

    def resolve(self, qname: str, rdtype: str, raise_on_no_answer: bool = True):
        key = (qname, rdtype)
        if key not in self.records:
            raise dns.resolver.NXDOMAIN()
        values = self.records[key]
        if not values:
            raise dns.resolver.NoAnswer()
        return [FakeRdata(v) for v in values]


class FakeNcResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content


_NC_NS = 'xmlns="http://api.namecheap.com/xml.response"'


def _nc_ok(body: str) -> FakeNcResponse:
    return FakeNcResponse(
        f'<?xml version="1.0" encoding="utf-8"?><ApiResponse {_NC_NS} Status="OK">{body}</ApiResponse>'.encode()
    )


@dataclass
class NamecheapState:
    domains: dict[str, dict] = field(default_factory=dict)
    check_available: dict[str, bool] = field(default_factory=dict)
    price: tuple[str, str] = ("10.98", "12.98")
    create_calls: list[str] = field(default_factory=list)


def install_namecheap(monkeypatch, state: NamecheapState) -> None:
    """Stubs namecheap.requests.get (its own import site) with an in-process fake XML API."""

    def fake_get(url, params=None, timeout=None):
        params = params or {}
        command = params.get("Command")
        if command == namecheap._CMD_GET_LIST:
            entries = "".join(
                f'<Domain Name="{name}" Expires="{info.get("expires", "09/21/2027")}" '
                f'IsLocked="{info.get("locked", "true")}" AutoRenew="{info.get("autorenew", "true")}" '
                f'WhoisGuard="{info.get("whoisguard", "ENABLED")}"/>'
                for name, info in state.domains.items()
            )
            return _nc_ok(f"<CommandResponse><DomainGetListResult>{entries}</DomainGetListResult></CommandResponse>")
        if command == namecheap._CMD_CHECK:
            names = params["DomainList"].split(",")
            entries = "".join(
                f'<DomainCheckResult Domain="{n}" Available="{"true" if state.check_available.get(n, True) else "false"}" '
                'IsPremiumName="false"/>'
                for n in names
            )
            return _nc_ok(f"<CommandResponse>{entries}</CommandResponse>")
        if command == namecheap._CMD_GET_PRICING:
            tld = params["ProductName"]
            register, renew = state.price
            return _nc_ok(
                '<CommandResponse><UserGetPricingResult><ProductType Name="DOMAIN">'
                f'<ProductCategory Name="register"><Product Name="{tld}">'
                f'<Price Duration="1" YourPrice="{register}" Currency="USD"/></Product></ProductCategory>'
                f'<ProductCategory Name="renew"><Product Name="{tld}">'
                f'<Price Duration="1" YourPrice="{renew}" Currency="USD"/></Product></ProductCategory>'
                "</ProductType></UserGetPricingResult></CommandResponse>"
            )
        if command == namecheap._CMD_CREATE:
            name = str(params.get("DomainName") or "")
            state.create_calls.append(name)
            return _nc_ok(
                f'<CommandResponse><DomainCreateResult Domain="{name}" Registered="true" '
                'ChargedAmount="10.98" OrderID="nc-e2e-order-1"/></CommandResponse>'
            )
        raise AssertionError(f"unexpected namecheap command {command!r}")

    monkeypatch.setattr(namecheap.requests, "get", fake_get)


class FakeGdResponse:
    def __init__(self, json_body: Any) -> None:
        self._json_body = json_body
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.content = b"1"

    def json(self) -> Any:
        return self._json_body


@dataclass
class GodaddyState:
    domains: list[dict] = field(default_factory=list)


def install_godaddy(monkeypatch, state: GodaddyState) -> None:
    """Stubs godaddy.requests.request (its own import site). Only list_domains is exercised here;
    an unexpected call (e.g. a godaddy purchase, which no scenario triggers) fails loudly."""

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        if method == "GET" and url.endswith(godaddy._DOMAINS_PATH):
            return FakeGdResponse(state.domains)
        raise AssertionError(f"unexpected godaddy call {method} {url}")

    monkeypatch.setattr(godaddy.requests, "request", fake_request)


def install_rdap(monkeypatch) -> None:
    """
    whoisit is a true external: lookup_rdap's domain_lookup/bootstrap/is_bootstrapped keywords
    default to the real whoisit.domain/bootstrap/is_bootstrapped, bound once at def time, so
    patching the whoisit module itself would not reach an already-bound default. Patching
    lookup_rdap's own __defaults__ is the seam pipeline/domains/rdap.py actually exposes.
    Every lookup reports not_found (a real whoisit.errors exception), keeping the "rdap" section
    stable across snapshots so it never masks the DNS-driven ns_changed assertion.
    """

    def fake_domain_lookup(name: str) -> dict:
        raise ResourceDoesNotExist("not found")

    monkeypatch.setattr(
        domain_rdap.lookup_rdap,
        "__defaults__",
        (fake_domain_lookup, lambda *a, **kw: None, lambda: True),
    )


# --- fixtures -------------------------------------------------------------------------------------


@dataclass
class Env:
    home: Path
    resolver: FakeResolver
    nc_state: NamecheapState
    gd_state: GodaddyState
    capture: Capture

    def app(self, headers: Optional[dict[str, str]] = None) -> "AppCtx":
        return AppCtx(self, headers)


class AppCtx:
    def __init__(self, env: "Env", headers: Optional[dict[str, str]]) -> None:
        self._env = env
        self._headers = WRITE if headers is None else headers
        self._client: Optional[TestClient] = None

    def __enter__(self) -> Recorder:
        self._client = TestClient(api_main.app, headers=self._headers)
        self._client.__enter__()
        return Recorder(self._client, self._env.capture)

    def __exit__(self, *exc: Any) -> None:
        assert self._client is not None
        self._client.__exit__(*exc)


@pytest.fixture
def env(monkeypatch, tmp_path) -> Iterator[Env]:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(api_main, "start_scheduler", lambda: None)
    (tmp_path / ".env").write_text("")

    key = crypto.generate_key()
    monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", key)

    resolver = FakeResolver()
    monkeypatch.setattr(domain_dns, "_make_resolver", lambda: resolver)
    install_rdap(monkeypatch)

    nc_state = NamecheapState()
    gd_state = GodaddyState()
    install_namecheap(monkeypatch, nc_state)
    install_godaddy(monkeypatch, gd_state)

    yield Env(tmp_path, resolver, nc_state, gd_state, Capture())

    connections.set_vault(None)
    health.set_env_overlay_provider(None, None)


def enable_purchasing(env: Env) -> None:
    """DOMAINS_PURCHASE_ENABLED is not in health._SINGLE_KEYS/prefix list, so it only ever comes
    from .env — written here, which _raw_env() re-reads on every call (no startup caching)."""
    (env.home / ".env").write_text("DOMAINS_PURCHASE_ENABLED=true\n")


def create_namecheap(client: Recorder, label: str = "ncwork") -> httpx.Response:
    return client.post(
        "/api/connections",
        json={
            "kind": "namecheap",
            "label": label,
            "api_user": "nc_user",
            "username": "nc_user",
            "client_ip": "203.0.113.5",
            "api_key": NAMECHEAP_API_KEY,
            "registrant_contact": REGISTRANT_CONTACT_JSON,
        },
    )


def create_godaddy(client: Recorder, label: str = "gdwork") -> httpx.Response:
    return client.post(
        "/api/connections",
        json={
            "kind": "godaddy",
            "label": label,
            "api_key": GODADDY_API_KEY,
            "api_secret": GODADDY_API_SECRET,
            "registrant_contact": REGISTRANT_CONTACT_JSON,
        },
    )


# --- scenario: CSRF -------------------------------------------------------------------------------


def test_mutating_domains_post_without_csrf_header_is_forbidden(env):
    with env.app(headers={}) as client:
        added = client.post("/api/domains", json={"name": "csrf-e2e.com"})
        assert added.status_code == 403
        synced = client.post("/api/domains/sync")
        assert synced.status_code == 403
        assert client.get("/api/domains").json() == []
    env.capture.assert_clean()


# --- scenario: sync lists both registrars' domains -------------------------------------------------


def test_sync_lists_domains_from_both_registrars(env):
    env.nc_state.domains = {"nc-alpha.com": {}}
    env.gd_state.domains = [
        {"domain": "gd-beta.com", "expires": "2027-09-21T00:00:00.000Z", "renewAuto": True, "locked": True}
    ]
    with env.app() as client:
        assert create_namecheap(client).status_code == 201
        assert create_godaddy(client).status_code == 201

        synced = client.post("/api/domains/sync")
        assert synced.status_code == 200
        sync_body = synced.json()
        assert {row["kind"] for row in sync_body["sync"]} == {"namecheap", "godaddy"}
        assert all(row["status"] == "ok" for row in sync_body["sync"])

        listed = {row["name"]: row for row in client.get("/api/domains").json()}
        assert listed["nc-alpha.com"]["source"] == "namecheap"
        assert listed["nc-alpha.com"]["ownership"] == "owned"
        assert listed["gd-beta.com"]["source"] == "godaddy"
        assert listed["gd-beta.com"]["ownership"] == "owned"
    env.capture.assert_clean()
    env.capture.assert_items_clean()


# --- scenario: purchase disabled by default -------------------------------------------------------


def test_purchase_refused_by_default(env):
    with env.app() as client:
        resp = client.post(
            "/api/domains/purchases",
            json={"quote_id": "no-such-quote", "confirm_name": "whatever.com", "years": 1},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "purchase_disabled"
    env.capture.assert_clean()
