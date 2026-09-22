"""
Unit tests for pipeline.jobs.ats.bamboohr and pipeline.jobs.ats.recruitee (match_url/probe/
list_postings).

No network: requests is stubbed at pipeline.jobs.ats.requests (both adapters call fetch_json,
which lives in pipeline.jobs.ats), following tests/test_ats_greenhouse_lever.py's route()/
sequence() convention. _sleep is stubbed the same way so unavailable-status retries never
actually wait.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.jobs import ats  # noqa: E402
from pipeline.jobs.ats import bamboohr, recruitee  # noqa: E402


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


# --- bamboohr: match_url -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.bamboohr.com/careers/list",
        "https://acme.bamboohr.com/careers/123",
        "https://acme.bamboohr.com/careers",
    ],
)
def test_bamboohr_match_url_extracts_company(url):
    assert bamboohr.adapter.match_url(url) == "acme"


def test_bamboohr_match_url_no_match_returns_none():
    assert bamboohr.adapter.match_url("https://boards.greenhouse.io/acme") is None
    assert bamboohr.adapter.match_url("https://example.com/careers") is None


# --- bamboohr: probe ----------------------------------------------------------------------


def test_bamboohr_probe_returns_board_info_on_valid_board(monkeypatch, sleeps):
    calls = route(
        monkeypatch, sequence(FakeResponse(json_data={"meta": {"totalCount": 0}, "result": []}))
    )
    info = bamboohr.adapter.probe("acme")
    assert info == ats.BoardInfo(board_id="acme", confidence=1.0)
    assert calls[0]["headers"]["Accept"] == "application/json"
    assert calls[0]["url"] == "https://acme.bamboohr.com/careers/list"


def test_bamboohr_probe_returns_none_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert bamboohr.adapter.probe("nope") is None


def test_bamboohr_probe_returns_none_on_malformed_2xx_body(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"not_result": []})))
    assert bamboohr.adapter.probe("acme") is None


def test_bamboohr_probe_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        bamboohr.adapter.probe("acme")


# --- bamboohr: list_postings ----------------------------------------------------------------


def test_bamboohr_list_postings_maps_fields(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "result": [
                        {
                            "id": 42,
                            "jobOpeningName": "Engineer",
                            "departmentLabel": "Engineering",
                            "locationLabel": "Remote",
                            "jobOpeningShareUrl": "https://acme.bamboohr.com/careers/42",
                        }
                    ]
                }
            )
        ),
    )
    postings = bamboohr.adapter.list_postings("acme")
    assert postings == [
        ats.RawPosting(
            external_id="42",
            title="Engineer",
            url="https://acme.bamboohr.com/careers/42",
            description="Department: Engineering | Location: Remote",
            location="Remote",
        )
    ]


def test_bamboohr_list_postings_falls_back_to_careers_list_url(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "result": [
                        {
                            "id": 42,
                            "jobOpeningName": "Engineer",
                        }
                    ]
                }
            )
        ),
    )
    postings = bamboohr.adapter.list_postings("acme")
    assert postings[0].url == "https://acme.bamboohr.com/careers/list"
    assert postings[0].description == ""


def test_bamboohr_list_postings_returns_empty_on_missing_result(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"meta": {}})))
    assert bamboohr.adapter.list_postings("acme") == []


def test_bamboohr_list_postings_returns_empty_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert bamboohr.adapter.list_postings("nope") == []


def test_bamboohr_list_postings_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        bamboohr.adapter.list_postings("acme")


# --- recruitee: match_url -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.recruitee.com/o/engineer",
        "https://acme.recruitee.com/api/offers/",
        "https://acme.recruitee.com",
    ],
)
def test_recruitee_match_url_extracts_company(url):
    assert recruitee.adapter.match_url(url) == "acme"


def test_recruitee_match_url_no_match_returns_none():
    assert recruitee.adapter.match_url("https://acme.bamboohr.com/careers") is None
    assert recruitee.adapter.match_url("https://example.com/careers") is None


# --- recruitee: probe ----------------------------------------------------------------------


def test_recruitee_probe_returns_board_info_on_valid_board(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"offers": []})))
    info = recruitee.adapter.probe("acme")
    assert info == ats.BoardInfo(board_id="acme", confidence=1.0)


def test_recruitee_probe_returns_none_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert recruitee.adapter.probe("nope") is None


def test_recruitee_probe_returns_none_on_malformed_2xx_body(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"unexpected": "shape"})))
    assert recruitee.adapter.probe("acme") is None


def test_recruitee_probe_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        recruitee.adapter.probe("acme")


# --- recruitee: list_postings ------------------------------------------------------------


def test_recruitee_list_postings_maps_fields(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "offers": [
                        {
                            "id": 7,
                            "title": "Engineer",
                            "careers_apply_url": "https://acme.recruitee.com/o/engineer",
                            "description": "<p>Do stuff</p>",
                            "city": "Amsterdam",
                            "country": "Netherlands",
                            "published_at": "2026-09-01T00:00:00Z",
                        }
                    ]
                }
            )
        ),
    )
    postings = recruitee.adapter.list_postings("acme")
    assert postings == [
        ats.RawPosting(
            external_id="7",
            title="Engineer",
            url="https://acme.recruitee.com/o/engineer",
            description="<p>Do stuff</p>",
            location="Amsterdam, Netherlands",
            posted_at="2026-09-01T00:00:00Z",
        )
    ]


def test_recruitee_list_postings_falls_back_to_created_at(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "offers": [
                        {
                            "id": 7,
                            "title": "Engineer",
                            "careers_apply_url": "https://acme.recruitee.com/o/engineer",
                            "description": "",
                            "created_at": "2026-08-01T00:00:00Z",
                        }
                    ]
                }
            )
        ),
    )
    postings = recruitee.adapter.list_postings("acme")
    assert postings[0].posted_at == "2026-08-01T00:00:00Z"
    assert postings[0].location is None


def test_recruitee_list_postings_returns_empty_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert recruitee.adapter.list_postings("nope") == []


def test_recruitee_list_postings_returns_empty_on_empty_offers(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"offers": []})))
    assert recruitee.adapter.list_postings("acme") == []


def test_recruitee_list_postings_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        recruitee.adapter.list_postings("acme")
