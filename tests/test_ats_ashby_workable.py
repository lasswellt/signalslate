"""
Unit tests for pipeline.jobs.ats.ashby and pipeline.jobs.ats.workable (match_url/probe/
list_postings).

No network: requests is stubbed at pipeline.jobs.ats.requests (both adapters call fetch_json,
which lives in pipeline.jobs.ats), following tests/test_jobs_ats_base.py's route()/sequence()
convention. _sleep is stubbed the same way so unavailable-status retries never actually wait.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.jobs import ats  # noqa: E402
from pipeline.jobs.ats import ashby, workable  # noqa: E402


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


# --- ashby: match_url --------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.ashbyhq.com/acme",
        "https://jobs.ashbyhq.com/acme?utm_source=x",
        "https://api.ashbyhq.com/posting-api/job-board/acme",
    ],
)
def test_ashby_match_url_extracts_board_name(url):
    assert ashby.adapter.match_url(url) == "acme"


def test_ashby_match_url_no_match_returns_none():
    assert ashby.adapter.match_url("https://boards.greenhouse.io/acme") is None
    assert ashby.adapter.match_url("https://example.com/careers") is None


# --- ashby: probe ------------------------------------------------------------------------


def test_ashby_probe_returns_board_info_on_valid_board(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"jobs": [], "name": "Acme Inc"})))
    info = ashby.adapter.probe("acme")
    assert info == ats.BoardInfo(board_id="acme", company_name="Acme Inc", confidence=1.0)


def test_ashby_probe_returns_none_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert ashby.adapter.probe("nope") is None


def test_ashby_probe_returns_none_on_org_not_found_shape(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"errors": ["Job board not found"]})))
    assert ashby.adapter.probe("nope") is None


def test_ashby_probe_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        ashby.adapter.probe("acme")


# --- ashby: list_postings ------------------------------------------------------------------


def test_ashby_list_postings_maps_fields(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "name": "Acme Inc",
                    "jobs": [
                        {
                            "id": "job-1",
                            "title": "Engineer",
                            "jobUrl": "https://jobs.ashbyhq.com/acme/job-1",
                            "location": "Remote",
                            "description": "<p>Do stuff</p>",
                            "publishedAt": "2026-09-01T00:00:00Z",
                            "isListed": True,
                        }
                    ],
                }
            )
        ),
    )
    postings = ashby.adapter.list_postings("acme")
    assert postings == [
        ats.RawPosting(
            external_id="job-1",
            title="Engineer",
            url="https://jobs.ashbyhq.com/acme/job-1",
            description="<p>Do stuff</p>",
            location="Remote",
            posted_at="2026-09-01T00:00:00Z",
        )
    ]


def test_ashby_list_postings_skips_unlisted_jobs(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "jobs": [
                        {
                            "id": "job-1",
                            "title": "Engineer",
                            "jobUrl": "https://jobs.ashbyhq.com/acme/job-1",
                            "isListed": False,
                        },
                        {
                            "id": "job-2",
                            "title": "Designer",
                            "jobUrl": "https://jobs.ashbyhq.com/acme/job-2",
                            "isListed": True,
                        },
                    ]
                }
            )
        ),
    )
    postings = ashby.adapter.list_postings("acme")
    assert [p.external_id for p in postings] == ["job-2"]


def test_ashby_list_postings_returns_empty_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert ashby.adapter.list_postings("nope") == []


def test_ashby_list_postings_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        ashby.adapter.list_postings("acme")


# --- workable: match_url -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://apply.workable.com/acme/",
        "https://apply.workable.com/acme",
        "https://apply.workable.com/acme/j/ABCDEF1234/",
    ],
)
def test_workable_match_url_extracts_account_subdomain(url):
    assert workable.adapter.match_url(url) == "acme"


def test_workable_match_url_no_match_returns_none():
    assert workable.adapter.match_url("https://jobs.lever.co/acme") is None
    assert workable.adapter.match_url("https://example.com/careers") is None
    assert workable.adapter.match_url("https://apply.workable.com/api/v1/widget/accounts/acme") is None


# --- workable: probe -----------------------------------------------------------------------


def test_workable_probe_returns_board_info_on_valid_board(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"name": "Acme Inc", "jobs": []})))
    info = workable.adapter.probe("acme")
    assert info == ats.BoardInfo(board_id="acme", company_name="Acme Inc", confidence=1.0)


def test_workable_probe_returns_none_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert workable.adapter.probe("nope") is None


def test_workable_probe_returns_none_on_malformed_2xx_body(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"unexpected": "shape"})))
    assert workable.adapter.probe("acme") is None


def test_workable_probe_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        workable.adapter.probe("acme")


# --- workable: list_postings ---------------------------------------------------------------


def test_workable_list_postings_maps_fields(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "name": "Acme Inc",
                    "jobs": [
                        {
                            "shortcode": "ABC123",
                            "title": "Engineer",
                            "url": "https://apply.workable.com/acme/j/ABC123/",
                            "description": "Do stuff",
                            "location": {"city": "Austin", "country": "United States"},
                        }
                    ],
                }
            )
        ),
    )
    postings = workable.adapter.list_postings("acme")
    assert postings == [
        ats.RawPosting(
            external_id="ABC123",
            title="Engineer",
            url="https://apply.workable.com/acme/j/ABC123/",
            description="Do stuff",
            location="Austin, United States",
        )
    ]


def test_workable_list_postings_formats_partial_location(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "jobs": [
                        {
                            "shortcode": "ABC123",
                            "title": "Engineer",
                            "url": "https://apply.workable.com/acme/j/ABC123/",
                            "location": {"country": "United States"},
                        }
                    ]
                }
            )
        ),
    )
    postings = workable.adapter.list_postings("acme")
    assert postings[0].location == "United States"


def test_workable_list_postings_returns_empty_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert workable.adapter.list_postings("nope") == []


def test_workable_list_postings_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        workable.adapter.list_postings("acme")
