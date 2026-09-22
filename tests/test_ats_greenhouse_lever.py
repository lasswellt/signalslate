"""
Unit tests for pipeline.jobs.ats.greenhouse and pipeline.jobs.ats.lever (match_url/probe/
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
from pipeline.jobs.ats import greenhouse, lever  # noqa: E402


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


# --- greenhouse: match_url --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/acme",
        "https://boards.greenhouse.io/acme/jobs/12345",
        "https://job-boards.greenhouse.io/acme?gh_src=abc",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
    ],
)
def test_greenhouse_match_url_extracts_slug(url):
    assert greenhouse.adapter.match_url(url) == "acme"


def test_greenhouse_match_url_no_match_returns_none():
    assert greenhouse.adapter.match_url("https://jobs.lever.co/acme") is None
    assert greenhouse.adapter.match_url("https://example.com/careers") is None


# --- greenhouse: probe -------------------------------------------------------------------


def test_greenhouse_probe_returns_board_info_on_valid_board(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"jobs": []})))
    info = greenhouse.adapter.probe("acme")
    assert info == ats.BoardInfo(board_id="acme", company_name=None, confidence=1.0)


def test_greenhouse_probe_returns_none_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert greenhouse.adapter.probe("nope") is None


def test_greenhouse_probe_returns_none_on_malformed_2xx_body(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"not_jobs": []})))
    assert greenhouse.adapter.probe("acme") is None


def test_greenhouse_probe_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        greenhouse.adapter.probe("acme")


# --- greenhouse: list_postings -------------------------------------------------------------


def test_greenhouse_list_postings_maps_fields(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "jobs": [
                        {
                            "id": 123,
                            "title": "Engineer",
                            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
                            "location": {"name": "Remote"},
                            "content": "<p>Do stuff</p>",
                            "updated_at": "2026-09-01T00:00:00Z",
                        }
                    ]
                }
            )
        ),
    )
    postings = greenhouse.adapter.list_postings("acme")
    assert postings == [
        ats.RawPosting(
            external_id="123",
            title="Engineer",
            url="https://boards.greenhouse.io/acme/jobs/123",
            description="<p>Do stuff</p>",
            location="Remote",
            posted_at="2026-09-01T00:00:00Z",
        )
    ]


def test_greenhouse_list_postings_returns_empty_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert greenhouse.adapter.list_postings("nope") == []


def test_greenhouse_list_postings_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        greenhouse.adapter.list_postings("acme")


# --- lever: match_url ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/acme",
        "https://jobs.lever.co/acme/abc-123-def",
        "https://api.lever.co/v0/postings/acme",
    ],
)
def test_lever_match_url_extracts_slug(url):
    assert lever.adapter.match_url(url) == "acme"


def test_lever_match_url_no_match_returns_none():
    assert lever.adapter.match_url("https://boards.greenhouse.io/acme") is None
    assert lever.adapter.match_url("https://example.com/careers") is None


# --- lever: probe --------------------------------------------------------------------------


def test_lever_probe_returns_board_info_on_valid_board(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data=[])))
    info = lever.adapter.probe("acme")
    assert info == ats.BoardInfo(board_id="acme", company_name=None, confidence=1.0)


def test_lever_probe_returns_none_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert lever.adapter.probe("nope") is None


def test_lever_probe_returns_none_on_malformed_2xx_body(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"unexpected": "shape"})))
    assert lever.adapter.probe("acme") is None


def test_lever_probe_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        lever.adapter.probe("acme")


# --- lever: list_postings ------------------------------------------------------------------


def test_lever_list_postings_maps_fields(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data=[
                    {
                        "id": "abc123",
                        "text": "Engineer",
                        "hostedUrl": "https://jobs.lever.co/acme/abc123",
                        "categories": {"location": "Remote"},
                        "descriptionPlain": "Do stuff",
                        "createdAt": 1_756_684_800_000,
                    }
                ]
            )
        ),
    )
    postings = lever.adapter.list_postings("acme")
    assert postings == [
        ats.RawPosting(
            external_id="abc123",
            title="Engineer",
            url="https://jobs.lever.co/acme/abc123",
            description="Do stuff",
            location="Remote",
            posted_at="2025-09-01T00:00:00+00:00",
        )
    ]


def test_lever_list_postings_falls_back_to_html_description(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data=[
                    {
                        "id": "abc123",
                        "text": "Engineer",
                        "hostedUrl": "https://jobs.lever.co/acme/abc123",
                        "categories": {},
                        "description": "<p>Do stuff</p>",
                        "createdAt": 1_756_684_800_000,
                    }
                ]
            )
        ),
    )
    postings = lever.adapter.list_postings("acme")
    assert postings[0].description == "<p>Do stuff</p>"
    assert postings[0].location is None


def test_lever_list_postings_returns_empty_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert lever.adapter.list_postings("nope") == []


def test_lever_list_postings_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        lever.adapter.list_postings("acme")
