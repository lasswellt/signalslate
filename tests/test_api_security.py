"""
Tests for api.security: origin allow-list, required header, JSON-only bodies, non-echoing 422.

Uses a router-only FastAPI app and a TestClient that is never entered as a context manager, so the
real lifespan (DB init, scheduler) does not run. The one test that imports api.main only checks
guard rejections, which happen before any handler.
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, SecretStr, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.security import install_security, normalize_origin  # noqa: E402

GOOD = {"X-Requested-With": "signalslate"}
ALLOWED = "https://ui.example.com"
SECRET = "SHORT-SECRET"
LONG_SECRET = "wrong-shape-secret-value-1234567890"


class SecretBody(BaseModel):
    label: str
    secret: SecretStr = Field(min_length=20)


class PairBody(BaseModel):
    a: SecretStr
    b: SecretStr

    @model_validator(mode="after")
    def _differ(self):
        if self.a.get_secret_value() == self.b.get_secret_value():
            raise ValueError("a and b must differ")
        return self


def build_app(allowed_origins: list[str]) -> FastAPI:
    app = FastAPI()
    install_security(app, allowed_origins=allowed_origins)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/thing")
    def read_thing():
        return {"thing": 1}

    @app.post("/api/thing")
    def create_thing(body: SecretBody):
        return {"label": body.label}

    @app.put("/api/thing")
    def put_thing(body: SecretBody):
        return {"label": body.label}

    @app.patch("/api/thing")
    def patch_thing(body: SecretBody):
        return {"label": body.label}

    @app.delete("/api/thing")
    def delete_thing():
        return {"deleted": True}

    @app.post("/api/run")
    def run_now():
        return {"ran": True}

    @app.post("/api/pair")
    def pair(body: PairBody):
        return {"ok": True}

    @app.post("/api/list")
    def items(body: list[SecretBody]):
        return {"n": len(body)}

    return app


@pytest.fixture
def client():
    return TestClient(build_app([ALLOWED]))


def valid_body() -> dict:
    return {"label": "a", "secret": "x" * 24}


# --- required header ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_missing_header_is_403_for_body_routes(client, method):
    resp = client.request(method, "/api/thing", json=valid_body())
    assert resp.status_code == 403


def test_missing_header_is_403_for_delete(client):
    assert client.delete("/api/thing").status_code == 403


def test_missing_header_is_403_for_bodyless_post(client):
    resp = client.post("/api/run")
    assert resp.status_code == 403


def test_wrong_header_value_is_403(client):
    resp = client.post("/api/run", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 403


def test_header_name_is_case_insensitive_value_is_exact(client):
    assert client.post("/api/run", headers={"x-requested-with": "signalslate"}).status_code == 200
    assert client.post("/api/run", headers={"x-requested-with": "SignalSlate"}).status_code == 403


def test_rejected_request_does_not_run_handler():
    calls: list[int] = []
    app = FastAPI()
    install_security(app, allowed_origins=[ALLOWED])

    @app.post("/api/run")
    def run_now():
        calls.append(1)
        return {}

    TestClient(app).post("/api/run")
    TestClient(app).post("/api/run", headers={**GOOD, "Origin": "https://evil.example.org"})
    assert calls == []


def test_header_without_origin_passes(client):
    resp = client.post("/api/run", headers=GOOD)
    assert resp.status_code == 200
    assert resp.json() == {"ran": True}


def test_all_methods_pass_with_header_and_allowed_origin(client):
    headers = {**GOOD, "Origin": ALLOWED}
    assert client.post("/api/thing", json=valid_body(), headers=headers).status_code == 200
    assert client.put("/api/thing", json=valid_body(), headers=headers).status_code == 200
    assert client.patch("/api/thing", json=valid_body(), headers=headers).status_code == 200
    assert client.delete("/api/thing", headers=headers).status_code == 200
    assert client.post("/api/run", headers=headers).status_code == 200


# --- origin allow-list ---------------------------------------------------------------------------


def test_wrong_origin_is_403_even_with_header(client):
    resp = client.post("/api/run", headers={**GOOD, "Origin": "https://evil.example.org"})
    assert resp.status_code == 403


def test_null_origin_never_matches(client):
    assert client.post("/api/run", headers={**GOOD, "Origin": "null"}).status_code == 403
    permissive = TestClient(build_app(["null", "*", ""]))
    assert permissive.post("/api/run", headers={**GOOD, "Origin": "null"}).status_code == 403
    assert permissive.post("/api/run", headers={**GOOD, "Origin": "https://evil.example.org"}).status_code == 403


@pytest.mark.parametrize("configured", [f"{ALLOWED}/", "https://UI.Example.COM", " https://ui.example.com// ", "https://ui.example.com:443"])
def test_configured_origin_variants_match_canonical_origin(configured):
    c = TestClient(build_app([configured]))
    assert c.post("/api/run", headers={**GOOD, "Origin": ALLOWED}).status_code == 200


def test_browser_origin_is_normalized_too():
    c = TestClient(build_app([ALLOWED]))
    assert c.post("/api/run", headers={**GOOD, "Origin": "HTTPS://UI.EXAMPLE.COM:443"}).status_code == 200


def test_default_port_ignored_for_http():
    c = TestClient(build_app(["http://lan.example.org:80/"]))
    assert c.post("/api/run", headers={**GOOD, "Origin": "http://lan.example.org"}).status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "https://ui.example.com:8443",
        "http://ui.example.com",
        "https://ui.example.com.evil.example.org",
        "https://ui.example.com@evil.example.org",
        "https://sub.ui.example.com",
    ],
)
def test_different_port_scheme_or_host_does_not_match(client, origin):
    assert client.post("/api/run", headers={**GOOD, "Origin": origin}).status_code == 403


def test_non_default_port_must_match_exactly():
    c = TestClient(build_app(["http://localhost:3000"]))
    assert c.post("/api/run", headers={**GOOD, "Origin": "http://localhost:3000"}).status_code == 200
    assert c.post("/api/run", headers={**GOOD, "Origin": "http://localhost:3001"}).status_code == 403
    assert c.post("/api/run", headers={**GOOD, "Origin": "http://localhost"}).status_code == 403


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://Example.com/", "https://example.com"),
        ("http://example.com:80", "http://example.com"),
        ("https://example.com:80", "https://example.com:80"),
        ("http://[::1]:3000", "http://[::1]:3000"),
        ("null", None),
        ("*", None),
        ("", None),
        ("ftp://example.com", None),
        ("https://example.com/path", None),
        ("https://user@example.com", None),
        ("https://example.com:notaport", None),
    ],
)
def test_normalize_origin(raw, expected):
    assert normalize_origin(raw) == expected


# --- exemptions ----------------------------------------------------------------------------------


def test_get_and_health_are_unaffected(client):
    assert client.get("/api/thing").status_code == 200
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/thing", headers={"Origin": "https://evil.example.org"}).status_code == 200


def test_preflight_from_allowed_origin(client):
    resp = client.options(
        "/api/thing",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with, content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED
    assert resp.headers["access-control-allow-credentials"] == "true"


def test_preflight_from_unlisted_origin_is_400_without_allow_origin(client):
    resp = client.options(
        "/api/thing",
        headers={
            "Origin": "https://evil.example.org",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with",
        },
    )
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers


def test_preflight_rejects_an_unlisted_header(client):
    resp = client.options(
        "/api/thing",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-anything-else",
        },
    )
    assert resp.status_code == 400


def test_guard_rejection_carries_cors_headers_for_allowed_origin(client):
    resp = client.post("/api/run", headers={"Origin": ALLOWED})
    assert resp.status_code == 403
    assert resp.headers["access-control-allow-origin"] == ALLOWED


def test_wildcard_in_config_is_dropped_not_honoured():
    c = TestClient(build_app(["*"]))
    resp = c.get("/api/thing", headers={"Origin": "https://evil.example.org"})
    assert "access-control-allow-origin" not in resp.headers
    assert "access-control-allow-credentials" not in resp.headers


def test_allowed_origin_gets_credentials_with_the_exact_origin_never_a_wildcard(client):
    resp = client.get("/api/thing", headers={"Origin": ALLOWED})
    assert resp.headers["access-control-allow-origin"] == ALLOWED
    assert resp.headers["access-control-allow-credentials"] == "true"
    write = client.post("/api/run", headers={**GOOD, "Origin": ALLOWED})
    assert write.headers["access-control-allow-origin"] == ALLOWED
    assert write.headers["access-control-allow-credentials"] == "true"


def test_unlisted_origin_gets_neither_allow_origin_nor_allow_credentials(client):
    evil = {"Origin": "https://evil.example.org"}
    preflight = client.options(
        "/api/thing",
        headers={**evil, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-requested-with"},
    )
    for resp in (client.get("/api/thing", headers=evil), preflight, client.post("/api/run", headers={**GOOD, **evil})):
        assert "access-control-allow-origin" not in resp.headers
        assert "access-control-allow-credentials" not in resp.headers


def test_wildcard_entry_is_dropped_but_a_listed_origin_beside_it_still_works():
    c = TestClient(build_app(["*", ALLOWED]))
    listed = c.get("/api/thing", headers={"Origin": ALLOWED})
    assert listed.headers["access-control-allow-origin"] == ALLOWED
    assert listed.headers["access-control-allow-credentials"] == "true"
    other = c.get("/api/thing", headers={"Origin": "https://evil.example.org"})
    assert "access-control-allow-origin" not in other.headers


# --- JSON-only bodies ----------------------------------------------------------------------------


def test_text_plain_body_is_415_and_not_echoed(client):
    resp = client.post("/api/thing", content=LONG_SECRET, headers={**GOOD, "Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert LONG_SECRET not in resp.text


def test_form_body_is_415_and_not_echoed(client):
    resp = client.post("/api/thing", data={"label": "a", "secret": LONG_SECRET}, headers=GOOD)
    assert resp.status_code == 415
    assert LONG_SECRET not in resp.text


def test_multipart_body_is_415(client):
    resp = client.post("/api/thing", files={"secret": ("s.txt", LONG_SECRET)}, headers=GOOD)
    assert resp.status_code == 415
    assert LONG_SECRET not in resp.text


def test_body_without_content_type_is_415(client):
    resp = client.post("/api/thing", content=b'{"label": "a"}', headers={**GOOD, "Content-Type": ""})
    assert resp.status_code == 415


def test_json_with_charset_parameter_is_accepted(client):
    resp = client.post(
        "/api/thing",
        content=b'{"label": "a", "secret": "' + b"x" * 24 + b'"}',
        headers={**GOOD, "Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 200


def test_bodyless_post_needs_no_content_type(client):
    assert client.post("/api/run", headers=GOOD).status_code == 200


# --- non-echoing 422 -----------------------------------------------------------------------------


def test_short_secret_422_does_not_echo_input(client):
    resp = client.post("/api/thing", json={"label": "a", "secret": SECRET}, headers=GOOD)
    assert resp.status_code == 422
    assert SECRET not in resp.text
    (error,) = resp.json()["detail"]
    assert set(error) == {"type", "loc", "msg"}
    assert error["type"] == "too_short"
    assert error["loc"] == ["body", "secret"]


def test_model_validator_422_does_not_echo_whole_body(client):
    body = {"a": "duplicate-secret-value", "b": "duplicate-secret-value"}
    resp = client.post("/api/pair", json=body, headers=GOOD)
    assert resp.status_code == 422
    assert "duplicate-secret-value" not in resp.text
    (error,) = resp.json()["detail"]
    assert set(error) == {"type", "loc", "msg"}
    assert error["type"] == "value_error"
    assert error["msg"] == "Value error, a and b must differ"


def test_wrong_json_shape_422_does_not_echo_body(client):
    resp = client.post("/api/thing", json=[LONG_SECRET], headers=GOOD)
    assert resp.status_code == 422
    assert LONG_SECRET not in resp.text
    assert all(set(e) == {"type", "loc", "msg"} for e in resp.json()["detail"])


def test_list_body_error_locations_but_no_input(client):
    resp = client.post("/api/list", json=[{"label": "a", "secret": SECRET}], headers=GOOD)
    assert resp.status_code == 422
    assert SECRET not in resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", 0, "secret"]


def test_missing_field_422_shape(client):
    resp = client.post("/api/thing", json={"secret": "x" * 24}, headers=GOOD)
    assert resp.status_code == 422
    assert resp.json() == {"detail": [{"type": "missing", "loc": ["body", "label"], "msg": "Field required"}]}


def test_json_decode_error_shape_does_not_echo_body(client):
    raw = b'{"label": "a", "secret": "' + LONG_SECRET.encode() + b'",}'
    resp = client.post("/api/thing", content=raw, headers={**GOOD, "Content-Type": "application/json"})
    assert resp.status_code == 422
    assert LONG_SECRET not in resp.text
    (error,) = resp.json()["detail"]
    assert set(error) == {"type", "loc", "msg"}
    assert error["type"] == "json_invalid"
    assert error["loc"][0] == "body"
    assert error["msg"] == "JSON decode error"


# --- the real app --------------------------------------------------------------------------------


def test_real_app_rejects_unguarded_writes_before_any_handler():
    from api.main import app

    c = TestClient(app)
    assert c.post("/api/runs/trigger").status_code == 403
    assert c.put("/api/config", json={}).status_code == 403
    evil = {**GOOD, "Origin": "https://evil.example.org"}
    assert c.post("/api/runs/trigger", headers=evil).status_code == 403
    preflight = c.options(
        "/api/runs/trigger",
        headers={"Origin": "https://evil.example.org", "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers
