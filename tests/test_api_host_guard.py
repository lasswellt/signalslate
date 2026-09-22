"""
Host header allow-list against DNS rebinding (check finding 6).

Two layers are covered. install_host_guard is exercised on scratch apps (router-only, never entered
as a context manager unless a test is about the lifespan scope), and api.main is used only for
requests the guard refuses or for GET /api/health, so no handler touches the real database.

The suite-wide "testserver" entry comes from tests/conftest.py (ALLOWED_HOSTS), which is what lets
every existing test that drives api.main.app with Starlette's TestClient run unchanged.
"""
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as api_main  # noqa: E402
from api.security import install_host_guard, install_security, normalize_host  # noqa: E402
from pipeline import health  # noqa: E402

REFUSED = {"detail": {"code": "host_not_allowed", "message": "Host not allowed"}}
GOOD = {"X-Requested-With": "signalslate"}


def build_app(allowed_hosts: list[str], calls: list[str] | None = None) -> FastAPI:
    """Scratch app whose handlers record that they ran, so a refusal can prove no handler executed."""
    seen = calls if calls is not None else []
    app = FastAPI()
    router = APIRouter()

    @router.get("/api/health")
    def health_route():
        seen.append("health")
        return {"ok": True}

    @router.get("/api/system")
    def system_route():
        seen.append("system")
        return {"secret_status": "readable"}

    @router.post("/api/thing")
    def create_thing():
        seen.append("create")
        return {"created": True}

    @router.websocket("/ws")
    async def ws_route(websocket: WebSocket):
        seen.append("ws")
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app.include_router(router)
    install_host_guard(app, allowed_hosts=allowed_hosts)
    return app


def client_for(app: FastAPI, host: str) -> TestClient:
    # An explicit Host header rather than base_url: the test client cannot parse an IPv6 base_url.
    return TestClient(app, headers={"Host": host})


def raw_http(app: FastAPI, headers: list[tuple[bytes, bytes]], path: str = "/api/system") -> tuple[int, bytes]:
    """Drives the ASGI app directly, for Host shapes an HTTP client will not send (none, duplicated)."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
        "client": ("203.0.113.9", 1234), "server": ("testserver", 8000),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body


# ---------------------------------------------------------------- normalize_host

@pytest.mark.parametrize(
    "value, expected",
    [
        ("example.com", "example.com"),
        ("Example.COM", "example.com"),
        ("example.com:8000", "example.com"),
        ("  example.com:8000  ", "example.com"),
        ("192.168.1.50:3000", "192.168.1.50"),
        ("my_host.lan", "my_host.lan"),
        ("[::1]", "::1"),
        ("[::1]:8000", "::1"),
        ("[FE80::1]:8000", "fe80::1"),
        ("::1", "::1"),
        ("localhost", "localhost"),
    ],
)
def test_normalize_host_accepts(value, expected):
    assert normalize_host(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "*",
        "*.example.com",
        "evil.*",
        "example.com:",
        "example.com:abc",
        "example.com:99999999",
        "example.com:80:80",
        "user@example.com",
        "http://example.com",
        "example.com/path",
        "exa mple.com",
        "-",
        "[::1",
        "[::1]x",
        "[::1]:abc",
        "[example.com]",
        "[::zz]",
        ":8000",
    ],
)
def test_normalize_host_rejects(value):
    assert normalize_host(value) is None


# ---------------------------------------------------------------- the guard on a scratch app

@pytest.mark.parametrize(
    "host",
    ["ui.example.com", "ui.example.com:8000", "UI.Example.COM", "Ui.Example.Com:443", "ui.example.com:1"],
)
def test_listed_host_reaches_the_handler_on_get_and_post(host):
    calls: list[str] = []
    client = client_for(build_app(["ui.example.com"], calls), host)
    assert client.get("/api/system").status_code == 200
    assert client.post("/api/thing").status_code == 200
    assert calls == ["system", "create"]


def test_configured_entry_case_and_port_are_normalized():
    client = client_for(build_app(["UI.Example.COM:8443"]), "ui.example.com:8000")
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("configured", [["::1"], ["[::1]"], ["[::1]:8000"]])
@pytest.mark.parametrize("host", ["[::1]:8000", "[::1]"])
def test_ipv6_literal_host_is_matched(configured, host):
    assert client_for(build_app(configured), host).get("/api/health").status_code == 200


def test_ipv6_literal_not_listed_is_refused():
    client = client_for(build_app(["ui.example.com", "127.0.0.1"]), "[::1]:8000")
    response = client.get("/api/system")
    assert response.status_code == 400
    assert response.json() == REFUSED


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8000", "EVIL.example:8000", "ui.example.com.evil.example", "xui.example.com"]
)
def test_unlisted_host_is_refused_on_get_and_post_without_running_a_handler(host):
    calls: list[str] = []
    client = client_for(build_app(["ui.example.com"], calls), host)
    for response in (client.get("/api/system"), client.post("/api/thing", headers=GOOD)):
        assert response.status_code == 400
        assert response.json() == REFUSED
    assert calls == []


def test_refusal_never_echoes_the_submitted_host():
    marker = "rebind-marker-7731.example"
    response = client_for(build_app(["ui.example.com"]), f"{marker}:8000").get("/api/system")
    assert response.status_code == 400
    assert marker not in response.text
    assert marker not in str(dict(response.headers))


def test_health_is_not_exempt():
    calls: list[str] = []
    client = client_for(build_app(["ui.example.com"], calls), "evil.example:8000")
    response = client.get("/api/health")
    assert response.status_code == 400
    assert response.json() == REFUSED
    assert calls == []


def test_missing_host_is_refused():
    status, body = raw_http(build_app(["ui.example.com"]), headers=[])
    assert status == 400
    assert b"host_not_allowed" in body


def test_duplicate_host_headers_are_refused_even_when_the_first_is_listed():
    app = build_app(["ui.example.com"])
    status, _ = raw_http(app, headers=[(b"host", b"ui.example.com"), (b"host", b"evil.example")])
    assert status == 400
    status, _ = raw_http(app, headers=[(b"host", b"evil.example"), (b"host", b"ui.example.com")])
    assert status == 400


@pytest.mark.parametrize("value", [b"ui.example.com:abc", b"ui.example.com@evil.example", b"ui.example.com/x", b"*", b""])
def test_malformed_host_is_refused(value):
    status, _ = raw_http(build_app(["ui.example.com", "*"]), headers=[(b"host", value)])
    assert status == 400


def test_listed_host_over_raw_asgi_is_allowed():
    status, body = raw_http(build_app(["ui.example.com"]), headers=[(b"host", b"UI.example.com:8000")])
    assert status == 200
    assert b"secret_status" in body


@pytest.mark.parametrize("entries", [["*"], ["*", "*.example.com", "evil.*"], [" * "], ["https://*.example.com"]])
def test_wildcard_entries_are_dropped_never_widened(entries):
    client = client_for(build_app(entries), "anything.example.com")
    assert client.get("/api/system").status_code == 400


def test_a_wildcard_does_not_remove_the_valid_entries_beside_it():
    app = build_app(["*", "ui.example.com"])
    assert client_for(app, "ui.example.com").get("/api/health").status_code == 200
    assert client_for(app, "other.example.com").get("/api/health").status_code == 400


def test_empty_allow_list_refuses_everything():
    assert client_for(build_app([]), "localhost").get("/api/health").status_code == 400


def test_websocket_with_unlisted_host_is_closed_before_the_handler():
    calls: list[str] = []
    client = client_for(build_app(["ui.example.com"], calls), "evil.example:8000")
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws"):
            pass
    assert excinfo.value.code == 1008
    assert calls == []


def test_websocket_with_listed_host_is_served():
    calls: list[str] = []
    client = client_for(build_app(["ui.example.com"], calls), "ui.example.com:8000")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hello"
    assert calls == ["ws"]


def test_lifespan_scope_passes_through_the_guard():
    started: list[str] = []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        started.append("up")
        yield
        started.append("down")

    app = FastAPI(lifespan=lifespan)
    install_host_guard(app, allowed_hosts=["ui.example.com"])
    with TestClient(app):
        assert started == ["up"]
    assert started == ["up", "down"]


def test_guard_is_outermost_a_preflight_for_an_unlisted_host_is_refused_without_cors_headers():
    app = build_app(["ui.example.com"])
    install_security(app, allowed_origins=["https://ui.example.com"])
    # install_host_guard ran first here, so re-add it last to mirror api.main's order.
    install_host_guard(app, allowed_hosts=["ui.example.com"])
    client = client_for(app, "evil.example:8000")
    response = client.options(
        "/api/thing",
        headers={"Origin": "https://ui.example.com", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400
    assert response.json() == REFUSED
    assert "access-control-allow-origin" not in response.headers

    listed = client_for(app, "ui.example.com").options(
        "/api/thing",
        headers={"Origin": "https://ui.example.com", "Access-Control-Request-Method": "POST"},
    )
    assert listed.status_code == 200
    assert listed.headers["access-control-allow-origin"] == "https://ui.example.com"


# ---------------------------------------------------------------- what api.main lists

def hosts_from_env(monkeypatch, **env: str) -> set[str]:
    """
    The effective allowed set: allowed_hosts() after install_host_guard drops the unusable entries.

    ALLOWED_HOSTS starts unset (conftest's suite-wide "testserver" would otherwise be in every result).
    """
    if "ALLOWED_HOSTS" not in env:
        monkeypatch.delenv("ALLOWED_HOSTS")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    hosts = api_main.allowed_hosts()
    return {host for host in (normalize_host(entry) for entry in hosts) if host is not None}


LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def test_defaults_are_loopback_and_the_dev_frontend(monkeypatch):
    assert hosts_from_env(monkeypatch) == LOOPBACK


def test_web_origins_hostnames_are_listed(monkeypatch):
    hosts = hosts_from_env(
        monkeypatch,
        WEB_ORIGINS="https://Ui.Example.com/, http://192.168.1.50:3000,http://[fd00::5]:3000",
    )
    assert hosts == LOOPBACK | {"ui.example.com", "192.168.1.50", "fd00::5"}


def test_web_origins_wildcards_are_dropped(monkeypatch):
    hosts = hosts_from_env(monkeypatch, WEB_ORIGINS="*, https://*.example.com, https://ui.example.com")
    assert hosts == LOOPBACK | {"ui.example.com"}


def test_public_base_url_host_is_listed(monkeypatch):
    hosts = hosts_from_env(monkeypatch, PUBLIC_BASE_URL="https://Digest.Example.org:8443/")
    assert hosts == LOOPBACK | {"digest.example.org"}


def test_public_base_url_that_is_not_https_adds_nothing(monkeypatch):
    assert hosts_from_env(monkeypatch, PUBLIC_BASE_URL="http://digest.example.org") == LOOPBACK


def test_lan_host_is_listed(monkeypatch):
    assert hosts_from_env(monkeypatch, LAN_HOST="digest-box.lan") == LOOPBACK | {"digest-box.lan"}


def test_allowed_hosts_setting_adds_entries_and_drops_wildcards(monkeypatch):
    hosts = hosts_from_env(monkeypatch, ALLOWED_HOSTS=" Proxy.Example.com , api:8000,*,*.example.org,, [::2] ")
    assert hosts == LOOPBACK | {"proxy.example.com", "api", "::2"}


def test_every_source_combines(monkeypatch):
    hosts = hosts_from_env(
        monkeypatch,
        WEB_ORIGINS="https://ui.example.com",
        PUBLIC_BASE_URL="https://public.example.org",
        LAN_HOST="192.168.1.50",
        ALLOWED_HOSTS="extra.example.net",
    )
    assert hosts == LOOPBACK | {"ui.example.com", "public.example.org", "192.168.1.50", "extra.example.net"}


def test_allowed_hosts_falls_back_to_the_env_file_and_the_process_environment_wins(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("ALLOWED_HOSTS=from-file.example.com\n")
    monkeypatch.setattr(health, "ROOT", tmp_path)
    assert hosts_from_env(monkeypatch) == LOOPBACK | {"from-file.example.com"}
    assert hosts_from_env(monkeypatch, ALLOWED_HOSTS="from-env.example.com") == LOOPBACK | {"from-env.example.com"}


def test_the_derived_list_drives_a_real_guard(monkeypatch):
    monkeypatch.setenv("WEB_ORIGINS", "https://ui.example.com")
    monkeypatch.setenv("LAN_HOST", "192.168.1.50")
    monkeypatch.delenv("ALLOWED_HOSTS")
    app = build_app(api_main.allowed_hosts())
    for host in ("ui.example.com", "192.168.1.50:8000", "localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
        assert client_for(app, host).get("/api/health").status_code == 200, host
    assert client_for(app, "evil.example:8000").get("/api/health").status_code == 400
    assert client_for(app, "testserver").get("/api/health").status_code == 400


# ---------------------------------------------------------------- the real app

def test_suite_wide_testserver_is_the_only_reason_existing_tests_pass():
    assert TestClient(api_main.app).get("/api/health").json() == {"ok": True}


@pytest.mark.parametrize(
    "path",
    ["/api/system", "/api/status", "/api/collectors/slack/items/1", "/api/collectors/slack/items", "/api/health"],
)
def test_rebinding_style_read_on_the_real_app_is_refused_before_any_router(path):
    client = TestClient(api_main.app, base_url="http://evil.example:8000")
    response = client.get(path)
    assert response.status_code == 400
    assert response.json() == REFUSED
    assert "evil.example" not in response.text


def test_unlisted_host_write_on_the_real_app_is_refused_even_with_the_csrf_header_and_a_listed_origin():
    client = TestClient(api_main.app, base_url="http://evil.example:8000")
    response = client.post("/api/runs", headers={**GOOD, "Origin": "http://localhost:3000"})
    assert response.status_code == 400
    assert response.json() == REFUSED


def test_origin_and_header_guard_still_answers_for_a_listed_host_on_the_real_app():
    client = TestClient(api_main.app)
    assert client.post("/api/runs").status_code == 403
    assert client.post("/api/runs", headers={**GOOD, "Origin": "https://evil.example"}).status_code == 403
