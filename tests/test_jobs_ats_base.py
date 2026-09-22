"""
Unit tests for pipeline.jobs.ats (AtsAdapter protocol shape, RawPosting/BoardInfo, get_adapter/
ADAPTER_KINDS/match_any_url, fetch_json).

No network: requests is stubbed at the module's own import site (monkeypatch.setattr(ats.requests,
"get", fake), following tests/test_domain_intel.py's route() convention). _sleep is stubbed the
same way so retries/backoff never actually wait.

The 11 per-ATS adapter modules land one at a time (T-005..T-010); get_adapter()/match_any_url()'s
"module not landed yet" path is exercised by forcing importlib.import_module to raise
ModuleNotFoundError rather than picking a currently-unimplemented kind by name, plus a fake module
injected into sys.modules for the "adapter exists" path.
"""
import sys
import types
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.jobs import ats  # noqa: E402


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, raise_on_json=False, headers=None):
        self._json = json_data
        self.status_code = status_code
        self._raise_on_json = raise_on_json
        self.headers = headers or {}

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._json


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(ats, "_sleep", calls.append)
    return calls


def route(monkeypatch, handler):
    """Stub ats.requests.get with handler(url, params, headers) -> FakeResponse; returns calls."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        return handler(url, params or {}, headers or {})

    monkeypatch.setattr(ats.requests, "get", fake_get)
    return calls


def sequence(*responses):
    """A handler that returns each response in order, raising if called more times than given."""
    remaining = list(responses)

    def handler(url, params, headers):
        return remaining.pop(0)

    return handler


def raise_sequence(*outcomes):
    """A handler yielding either an exception instance (raised) or a response, in order."""
    remaining = list(outcomes)

    def handler(url, params, headers):
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return handler


# --- value objects / ADAPTER_KINDS ----------------------------------------------------


def test_adapter_kinds_has_all_eleven_kinds():
    assert ats.ADAPTER_KINDS == (
        "greenhouse",
        "lever",
        "ashby",
        "workable",
        "workday",
        "smartrecruiters",
        "rippling",
        "bamboohr",
        "recruitee",
        "personio",
        "jsonld",
    )
    assert len(set(ats.ADAPTER_KINDS)) == 11


def test_raw_posting_defaults():
    posting = ats.RawPosting(external_id="123", title="Engineer", url="https://x.example/123")
    assert posting.description == ""
    assert posting.location is None
    assert posting.remote is None
    assert posting.comp_text is None
    assert posting.posted_at is None


def test_board_info_defaults():
    info = ats.BoardInfo(board_id="acme")
    assert info.company_name is None
    assert info.confidence == 1.0


# --- get_adapter / match_any_url -------------------------------------------------------


def test_get_adapter_rejects_unknown_kind():
    with pytest.raises(ValueError):
        ats.get_adapter("not_a_real_ats")


def test_get_adapter_raises_module_not_found_for_unimplemented_kind(monkeypatch):
    # A valid ADAPTER_KINDS entry whose module hasn't landed (or has been removed) still surfaces
    # ModuleNotFoundError rather than being swallowed. Forced via importlib rather than picking a
    # currently-unimplemented kind by name, since T-005..T-010 land the real modules one at a time
    # and a hardcoded kind here would go stale as each one ships.
    def fake_import(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(ats.importlib, "import_module", fake_import)
    with pytest.raises(ModuleNotFoundError):
        ats.get_adapter("greenhouse")


def test_get_adapter_returns_module_adapter_when_present(monkeypatch):
    fake_module = types.ModuleType("pipeline.jobs.ats.greenhouse")
    fake_module.adapter = object()
    monkeypatch.setitem(sys.modules, "pipeline.jobs.ats.greenhouse", fake_module)
    assert ats.get_adapter("greenhouse") is fake_module.adapter


def test_match_any_url_skips_unimplemented_kinds_and_returns_none(monkeypatch):
    # Forced via importlib (see test_get_adapter_raises_module_not_found_for_unimplemented_kind)
    # so this doesn't go stale as T-005..T-010 land real adapter modules.
    def fake_import(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(ats.importlib, "import_module", fake_import)
    assert ats.match_any_url("https://boards.greenhouse.io/acme") is None


def test_match_any_url_returns_first_match(monkeypatch):
    class FakeAdapter:
        def match_url(self, url):
            return "acme" if "acme" in url else None

    fake_module = types.ModuleType("pipeline.jobs.ats.lever")
    fake_module.adapter = FakeAdapter()
    monkeypatch.setitem(sys.modules, "pipeline.jobs.ats.lever", fake_module)

    result = ats.match_any_url("https://jobs.lever.co/acme")
    assert result == ("lever", "acme")


def test_match_any_url_no_match_returns_none(monkeypatch):
    class FakeAdapter:
        def match_url(self, url):
            return None

    fake_module = types.ModuleType("pipeline.jobs.ats.ashby")
    fake_module.adapter = FakeAdapter()
    monkeypatch.setitem(sys.modules, "pipeline.jobs.ats.ashby", fake_module)

    assert ats.match_any_url("https://example.com/careers") is None


# --- fetch_json: ok / retry / backoff / Retry-After / errors ---------------------------


def test_fetch_json_ok_sends_descriptive_user_agent(monkeypatch, sleeps):
    calls = route(monkeypatch, sequence(FakeResponse(json_data={"jobs": []})))
    data, status, error = ats.fetch_json("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    assert status == "ok"
    assert data == {"jobs": []}
    assert error is None
    assert calls[0]["headers"]["User-Agent"] == ats._USER_AGENT
    assert "SignalSlate" in ats._USER_AGENT
    assert sleeps == []


def test_fetch_json_merges_extra_headers_without_dropping_user_agent(monkeypatch, sleeps):
    calls = route(monkeypatch, sequence(FakeResponse(json_data={})))
    ats.fetch_json("https://x.example/jobs", headers={"Accept": "application/json"})
    assert calls[0]["headers"]["User-Agent"] == ats._USER_AGENT
    assert calls[0]["headers"]["Accept"] == "application/json"


def test_fetch_json_retries_retryable_status_then_succeeds(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(status_code=503),
            FakeResponse(json_data={"ok": True}),
        ),
    )
    data, status, error = ats.fetch_json("https://x.example/jobs")
    assert status == "ok"
    assert data == {"ok": True}
    assert sleeps == [ats._BACKOFF_SECONDS]


def test_fetch_json_honors_retry_after_header_over_fixed_backoff(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(status_code=429, headers={"Retry-After": "17"}),
            FakeResponse(json_data={"ok": True}),
        ),
    )
    data, status, error = ats.fetch_json("https://x.example/jobs")
    assert status == "ok"
    assert sleeps == [17.0]


def test_fetch_json_falls_back_to_fixed_backoff_when_retry_after_unparseable(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(status_code=503, headers={"Retry-After": "not-a-number"}),
            FakeResponse(json_data={"ok": True}),
        ),
    )
    ats.fetch_json("https://x.example/jobs")
    assert sleeps == [ats._BACKOFF_SECONDS]


def test_fetch_json_unavailable_after_exhausting_retries(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(status_code=503),
            FakeResponse(status_code=503),
            FakeResponse(status_code=503),
        ),
    )
    data, status, error = ats.fetch_json("https://x.example/jobs")
    assert data is None
    assert status == "unavailable"
    assert error == "HTTP503"
    assert len(sleeps) == ats._MAX_ATTEMPTS - 1


def test_fetch_json_unavailable_on_connection_error(monkeypatch, sleeps):
    route(monkeypatch, raise_sequence(requests.ConnectionError("boom"), requests.ConnectionError("boom"), requests.ConnectionError("boom")))
    data, status, error = ats.fetch_json("https://x.example/jobs")
    assert data is None
    assert status == "unavailable"
    assert error == "ConnectionError"


def test_fetch_json_error_on_non_retryable_status(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    data, status, error = ats.fetch_json("https://x.example/jobs")
    assert data is None
    assert status == "error"
    assert error == "HTTP404"
    assert sleeps == []


def test_fetch_json_error_on_invalid_json(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(raise_on_json=True)))
    data, status, error = ats.fetch_json("https://x.example/jobs")
    assert data is None
    assert status == "error"
    assert error == "ValueError"
