"""
Unit tests for pipeline.domains.intel (subdomains, archived_urls).

No network: requests is stubbed at the module's own import site (monkeypatch.setattr(intel.requests,
"get", fake), following tests/test_collectors.py's route() convention). _sleep is stubbed the same
way pipeline.collectors.gmail's tests stub its backoff, so retries never actually wait.
"""
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.domains import intel  # noqa: E402

NAME = "example.com"


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, raise_on_json=False):
        self._json = json_data
        self.status_code = status_code
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._json


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(intel, "_sleep", calls.append)
    return calls


def route(monkeypatch, handler):
    """Stub intel.requests.get with handler(url, params) -> FakeResponse; returns the call list."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params or {}})
        return handler(url, params or {})

    monkeypatch.setattr(intel.requests, "get", fake_get)
    return calls


def sequence(*responses):
    """A handler that returns each response in order, raising if called more times than given."""
    remaining = list(responses)

    def handler(url, params):
        return remaining.pop(0)

    return handler


def raise_sequence(*outcomes):
    """A handler yielding either an exception instance (raised) or a response, in order."""
    remaining = list(outcomes)

    def handler(url, params):
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return handler


# --- subdomains: ok, dedupe, wildcard strip, ownership filter ------------------------


def test_subdomains_ok_dedupes_strips_wildcards_and_filters_by_ownership(monkeypatch, sleeps):
    body = [
        {"name_value": "www.example.com\n*.example.com"},
        {"name_value": "WWW.EXAMPLE.COM"},
        {"name_value": "api.example.com"},
        {"name_value": "example.com"},
        {"name_value": "not-example.com"},
        {"name_value": "example.org"},
        {"other_key": "ignored"},
    ]
    calls = route(monkeypatch, sequence(FakeResponse(body, 200)))

    result = intel.subdomains(NAME)

    assert result.status == "ok"
    assert result.error is None
    assert result.names == ("api.example.com", "example.com", "www.example.com")
    assert sleeps == []
    assert calls[0]["params"] == {"q": "%.example.com", "output": "json"}
    assert calls[0]["url"] == intel._CRTSH_URL


def test_subdomains_caps_at_max(monkeypatch, sleeps):
    body = [{"name_value": f"sub{i}.example.com"} for i in range(intel._MAX_SUBDOMAINS + 50)]
    route(monkeypatch, sequence(FakeResponse(body, 200)))

    result = intel.subdomains(NAME)

    assert result.status == "ok"
    assert len(result.names) == intel._MAX_SUBDOMAINS


def test_subdomains_non_dict_entries_and_non_string_name_value_are_skipped(monkeypatch, sleeps):
    body = ["not-a-dict", {"name_value": 123}, {"name_value": "sub.example.com"}]
    route(monkeypatch, sequence(FakeResponse(body, 200)))

    result = intel.subdomains(NAME)

    assert result.status == "ok"
    assert result.names == ("sub.example.com",)


# --- subdomains: retry then unavailable -----------------------------------------------


def test_subdomains_retries_once_then_succeeds(monkeypatch, sleeps):
    body = [{"name_value": "sub.example.com"}]
    calls = route(monkeypatch, sequence(FakeResponse(None, 502), FakeResponse(body, 200)))

    result = intel.subdomains(NAME)

    assert result.status == "ok"
    assert result.names == ("sub.example.com",)
    assert len(calls) == 2
    assert sleeps == [intel._BACKOFF_SECONDS]


def test_subdomains_unavailable_after_retry_exhausted_on_repeated_502(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(None, 502), FakeResponse(None, 502)))

    result = intel.subdomains(NAME)

    assert result.status == "unavailable"
    assert result.names == ()
    assert result.error == "HTTP502"
    assert sleeps == [intel._BACKOFF_SECONDS]


def test_subdomains_unavailable_on_repeated_timeout(monkeypatch, sleeps):
    route(monkeypatch, raise_sequence(requests.Timeout("slow"), requests.Timeout("slow")))

    result = intel.subdomains(NAME)

    assert result.status == "unavailable"
    assert result.error == "Timeout"


def test_subdomains_error_on_non_retryable_http_status(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(None, 400)))

    result = intel.subdomains(NAME)

    assert result.status == "error"
    assert result.error == "HTTP400"
    assert sleeps == []


def test_subdomains_error_on_malformed_json(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(raise_on_json=True, status_code=200)))

    result = intel.subdomains(NAME)

    assert result.status == "error"
    assert result.error == "ValueError"


def test_subdomains_error_on_unexpected_response_shape(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse({"unexpected": "shape"}, 200)))

    result = intel.subdomains(NAME)

    assert result.status == "error"
    assert result.error == "UnexpectedResponseShape"


# --- archived_urls: ok, dedupe, ownership filter --------------------------------------


CDX_HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]


def _cdx_row(original):
    return ["key", "20200101000000", original, "text/html", "200", "digest", "100"]


def test_archived_urls_ok_filters_by_ownership_and_dedupes(monkeypatch, sleeps):
    body = [
        CDX_HEADER,
        _cdx_row("https://example.com/"),
        _cdx_row("https://www.example.com/page"),
        _cdx_row("https://example.com/"),
        _cdx_row("https://not-example.com/"),
        _cdx_row("https://example.org/"),
    ]
    calls = route(monkeypatch, sequence(FakeResponse(body, 200)))

    result = intel.archived_urls(NAME)

    assert result.status == "ok"
    assert result.error is None
    assert result.urls == ("https://example.com/", "https://www.example.com/page")
    assert calls[0]["url"] == intel._WAYBACK_CDX_URL
    assert calls[0]["params"] == {
        "url": "example.com/*",
        "output": "json",
        "collapse": "urlkey",
        "limit": str(intel._WAYBACK_LIMIT),
    }


def test_archived_urls_empty_result_set_is_ok(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse([], 200)))

    result = intel.archived_urls(NAME)

    assert result.status == "ok"
    assert result.urls == ()
    assert result.error is None


def test_archived_urls_caps_at_limit(monkeypatch, sleeps):
    body = [CDX_HEADER] + [_cdx_row(f"https://example.com/{i}") for i in range(intel._WAYBACK_LIMIT + 50)]
    route(monkeypatch, sequence(FakeResponse(body, 200)))

    result = intel.archived_urls(NAME)

    assert result.status == "ok"
    assert len(result.urls) == intel._WAYBACK_LIMIT


def test_archived_urls_skips_rows_with_unparseable_or_foreign_host(monkeypatch, sleeps):
    body = [
        CDX_HEADER,
        ["key", "20200101000000", "not a valid url ://", "text/html", "200", "digest", "1"],
        _cdx_row("https://sub.example.com/ok"),
    ]
    route(monkeypatch, sequence(FakeResponse(body, 200)))

    result = intel.archived_urls(NAME)

    assert result.status == "ok"
    assert result.urls == ("https://sub.example.com/ok",)


# --- archived_urls: retry, unavailable, error -----------------------------------------


def test_archived_urls_retries_once_then_succeeds(monkeypatch, sleeps):
    body = [CDX_HEADER, _cdx_row("https://example.com/")]
    calls = route(monkeypatch, sequence(FakeResponse(None, 503), FakeResponse(body, 200)))

    result = intel.archived_urls(NAME)

    assert result.status == "ok"
    assert len(calls) == 2
    assert sleeps == [intel._BACKOFF_SECONDS]


def test_archived_urls_unavailable_after_retry_exhausted(monkeypatch, sleeps):
    route(monkeypatch, raise_sequence(requests.ConnectionError("down"), requests.ConnectionError("down")))

    result = intel.archived_urls(NAME)

    assert result.status == "unavailable"
    assert result.urls == ()
    assert result.error == "ConnectionError"


def test_archived_urls_error_on_missing_original_column(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse([["urlkey", "timestamp"]], 200)))

    result = intel.archived_urls(NAME)

    assert result.status == "error"
    assert result.error == "UnexpectedResponseShape"


def test_archived_urls_error_on_non_list_response(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse({"unexpected": "shape"}, 200)))

    result = intel.archived_urls(NAME)

    assert result.status == "error"
    assert result.error == "UnexpectedResponseShape"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
