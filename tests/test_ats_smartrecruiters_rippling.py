"""
Unit tests for pipeline.jobs.ats.smartrecruiters and pipeline.jobs.ats.rippling (match_url/probe/
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
from pipeline.jobs.ats import rippling, smartrecruiters  # noqa: E402


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


# --- smartrecruiters: match_url -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.smartrecruiters.com/acme/1234-engineer",
        "https://api.smartrecruiters.com/v1/companies/acme/postings",
    ],
)
def test_sr_match_url_extracts_company_id(url):
    assert smartrecruiters.adapter.match_url(url) == "acme"


def test_sr_match_url_no_match_returns_none():
    assert smartrecruiters.adapter.match_url("https://boards.greenhouse.io/acme") is None
    assert smartrecruiters.adapter.match_url("https://example.com/careers") is None


# --- smartrecruiters: probe ------------------------------------------------------------------


def test_sr_probe_returns_board_info_on_valid_board(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "totalFound": 1,
                    "content": [{"id": "1", "name": "Engineer", "company": {"name": "Acme"}}],
                }
            )
        ),
    )
    info = smartrecruiters.adapter.probe("acme")
    assert info == ats.BoardInfo(board_id="acme", company_name="Acme", confidence=1.0)


def test_sr_probe_returns_none_on_total_found_zero(monkeypatch, sleeps):
    """SmartRecruiters 200s an unknown company_id with totalFound: 0 rather than 404ing."""
    route(monkeypatch, sequence(FakeResponse(json_data={"totalFound": 0, "content": []})))
    assert smartrecruiters.adapter.probe("nope") is None


def test_sr_probe_returns_none_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert smartrecruiters.adapter.probe("nope") is None


def test_sr_probe_returns_none_on_malformed_2xx_body(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"unexpected": "shape"})))
    assert smartrecruiters.adapter.probe("acme") is None


def test_sr_probe_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        smartrecruiters.adapter.probe("acme")


# --- smartrecruiters: list_postings ------------------------------------------------------------


def test_sr_list_postings_maps_fields_and_fetches_description(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "totalFound": 1,
                    "content": [
                        {
                            "id": "123",
                            "name": "Engineer",
                            "releasedDate": "2026-09-01T00:00:00Z",
                            "location": {"city": "Berlin", "region": "BE", "country": "de"},
                            "company": {"name": "Acme"},
                        }
                    ],
                }
            ),
            FakeResponse(
                json_data={
                    "jobAd": {
                        "sections": {
                            "jobDescription": {"text": "Do stuff"},
                            "qualifications": {"text": "Know things"},
                        }
                    }
                }
            ),
        ),
    )
    postings = smartrecruiters.adapter.list_postings("acme")
    assert postings == [
        ats.RawPosting(
            external_id="123",
            title="Engineer",
            url="https://jobs.smartrecruiters.com/acme/123",
            description="Do stuff\n\nKnow things",
            location="Berlin, BE, de",
            posted_at="2026-09-01T00:00:00Z",
        )
    ]


def test_sr_list_postings_pages_until_short_page(monkeypatch, sleeps):
    full_page = [
        {"id": str(i), "name": f"Job {i}", "releasedDate": None, "location": {}}
        for i in range(smartrecruiters._PAGE_LIMIT)
    ]
    short_page = [{"id": "last", "name": "Last Job", "releasedDate": None, "location": {}}]

    responses = []

    def page_response(jobs, total):
        return FakeResponse(json_data={"totalFound": total, "content": jobs})

    def detail_response():
        return FakeResponse(json_data={"jobAd": {"sections": {}}})

    total = smartrecruiters._PAGE_LIMIT + 1
    responses.append(page_response(full_page, total))
    for _ in full_page:
        responses.append(detail_response())
    responses.append(page_response(short_page, total))
    responses.append(detail_response())

    route(monkeypatch, sequence(*responses))
    postings = smartrecruiters.adapter.list_postings("acme")
    assert len(postings) == smartrecruiters._PAGE_LIMIT + 1
    assert postings[-1].external_id == "last"


def test_sr_list_postings_returns_empty_on_total_found_zero(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"totalFound": 0, "content": []})))
    assert smartrecruiters.adapter.list_postings("nope") == []


def test_sr_list_postings_returns_empty_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert smartrecruiters.adapter.list_postings("nope") == []


def test_sr_list_postings_description_empty_on_detail_flake(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "totalFound": 1,
                    "content": [{"id": "1", "name": "Engineer", "location": {}}],
                }
            ),
            FakeResponse(status_code=404),
        ),
    )
    postings = smartrecruiters.adapter.list_postings("acme")
    assert postings[0].description == ""


def test_sr_list_postings_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        smartrecruiters.adapter.list_postings("acme")


# --- rippling: match_url ---------------------------------------------------------------------


def test_rippling_match_url_extracts_slug():
    url = "https://ats.rippling.com/acme/jobs/1234-engineer"
    assert rippling.adapter.match_url(url) == "acme"


def test_rippling_match_url_no_match_returns_none():
    assert rippling.adapter.match_url("https://boards.greenhouse.io/acme") is None
    assert rippling.adapter.match_url("https://ats.rippling.com/acme") is None
    assert rippling.adapter.match_url("https://example.com/careers") is None


# --- rippling: probe --------------------------------------------------------------------------


def test_rippling_probe_returns_board_info_on_valid_board(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"items": []})))
    info = rippling.adapter.probe("acme")
    assert info == ats.BoardInfo(board_id="acme", company_name=None, confidence=1.0)


def test_rippling_probe_returns_none_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert rippling.adapter.probe("nope") is None


def test_rippling_probe_returns_none_on_malformed_2xx_body(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(json_data={"unexpected": "shape"})))
    assert rippling.adapter.probe("acme") is None


def test_rippling_probe_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        rippling.adapter.probe("acme")


# --- rippling: list_postings -------------------------------------------------------------------


def test_rippling_list_postings_maps_fields(monkeypatch, sleeps):
    route(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "items": [
                        {
                            "id": "abc123",
                            "name": "Business Operations Manager",
                            "url": "https://ats.rippling.com/acme/jobs/abc123",
                            "department": "Operations",
                            "locations": [{"name": "Remote"}, "San Francisco, CA"],
                        }
                    ]
                }
            )
        ),
    )
    postings = rippling.adapter.list_postings("acme")
    assert postings == [
        ats.RawPosting(
            external_id="abc123",
            title="Business Operations Manager",
            url="https://ats.rippling.com/acme/jobs/abc123",
            description="Department: Operations | Locations: Remote, San Francisco, CA",
            location="Remote, San Francisco, CA",
            posted_at=None,
        )
    ]


def test_rippling_list_postings_returns_empty_on_404(monkeypatch, sleeps):
    route(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert rippling.adapter.list_postings("nope") == []


def test_rippling_list_postings_raises_on_unavailable(monkeypatch, sleeps):
    route(monkeypatch, sequence(*([FakeResponse(status_code=503)] * ats._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        rippling.adapter.list_postings("acme")
