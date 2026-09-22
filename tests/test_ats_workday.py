"""
Unit tests for pipeline.jobs.ats.workday (match_url/probe/list_postings).

No network: workday.requests.post (the jobs-list POST) and pipeline.jobs.ats.requests.get (the
per-posting description GET, via fetch_json) are both stubbed, following
tests/test_ats_greenhouse_lever.py's route()/sequence() convention. Both modules' _sleep are
stubbed the same way so unavailable-status retries never actually wait.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.jobs import ats  # noqa: E402
from pipeline.jobs.ats import workday  # noqa: E402


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
    monkeypatch.setattr(workday, "_sleep", calls.append)
    monkeypatch.setattr(ats, "_sleep", calls.append)
    return calls


def route_post(monkeypatch, handler):
    """Stub workday.requests.post with handler(url, payload, headers) -> FakeResponse."""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "payload": json or {}, "headers": headers or {}})
        return handler(url, json or {}, headers or {})

    monkeypatch.setattr(workday.requests, "post", fake_post)
    return calls


def route_get(monkeypatch, handler):
    """Stub ats.requests.get with handler(url, params, headers) -> FakeResponse (used by
    workday._fetch_description's fetch_json() call)."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        return handler(url, params or {}, headers or {})

    monkeypatch.setattr(ats.requests, "get", fake_get)
    return calls


def sequence(*responses):
    """A handler that returns each response in order, raising if called more times than given."""
    remaining = list(responses)

    def handler(*args, **kwargs):
        return remaining.pop(0)

    return handler


# --- match_url ---------------------------------------------------------------------------


def test_match_url_extracts_tenant_wdn_site():
    url = "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Software-Engineer_R-12345"
    assert workday.adapter.match_url(url) == "acme|wd5|External"


def test_match_url_skips_locale_segment():
    url = "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Engineer_R-1"
    assert workday.adapter.match_url(url) == "acme|wd1|External"


def test_match_url_no_match_returns_none():
    assert workday.adapter.match_url("https://boards.greenhouse.io/acme") is None
    assert workday.adapter.match_url("https://acme.myworkdayjobs.com/External") is None
    assert workday.adapter.match_url("https://acme.wd5.myworkdayjobs.com") is None
    assert workday.adapter.match_url("not a url") is None


# --- probe ---------------------------------------------------------------------------------


def test_probe_returns_board_info_on_valid_board(monkeypatch, sleeps):
    route_post(
        monkeypatch,
        sequence(FakeResponse(json_data={"total": 0, "jobPostings": []})),
    )
    info = workday.adapter.probe("acme|wd5|External")
    assert info == ats.BoardInfo(board_id="acme|wd5|External", company_name=None, confidence=1.0)


def test_probe_returns_none_on_404(monkeypatch, sleeps):
    route_post(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert workday.adapter.probe("acme|wd5|External") is None


def test_probe_returns_none_on_malformed_2xx_body(monkeypatch, sleeps):
    route_post(monkeypatch, sequence(FakeResponse(json_data={"unexpected": "shape"})))
    assert workday.adapter.probe("acme|wd5|External") is None


def test_probe_returns_none_on_malformed_board_id(monkeypatch, sleeps):
    assert workday.adapter.probe("acme|wd5") is None
    assert workday.adapter.probe("acme||External") is None
    assert workday.adapter.probe("not-a-triple") is None


def test_probe_raises_on_unavailable(monkeypatch, sleeps):
    route_post(monkeypatch, sequence(*([FakeResponse(status_code=503)] * workday._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        workday.adapter.probe("acme|wd5|External")


def test_probe_posts_expected_payload(monkeypatch, sleeps):
    calls = route_post(
        monkeypatch,
        sequence(FakeResponse(json_data={"total": 0, "jobPostings": []})),
    )
    workday.adapter.probe("acme|wd5|External")
    assert len(calls) == 1
    assert calls[0]["url"] == "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    assert calls[0]["payload"] == {
        "appliedFacets": {},
        "limit": 1,
        "offset": 0,
        "searchText": "",
    }
    assert calls[0]["headers"]["Content-Type"] == "application/json"


# --- list_postings ---------------------------------------------------------------------------


def test_list_postings_maps_fields_and_fetches_description(monkeypatch, sleeps):
    route_post(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "total": 1,
                    "jobPostings": [
                        {
                            "title": "Engineer",
                            "externalPath": "/job/Remote/Software-Engineer_R-12345",
                            "locationsText": "Remote",
                            "postedOn": "Posted 3 Days Ago",
                            "bulletFields": ["R-12345"],
                        }
                    ],
                }
            )
        ),
    )
    route_get(
        monkeypatch,
        sequence(
            FakeResponse(json_data={"jobPostingInfo": {"jobDescription": "<p>Do stuff</p>"}})
        ),
    )
    postings = workday.adapter.list_postings("acme|wd5|External")
    assert postings == [
        ats.RawPosting(
            external_id="R-12345",
            title="Engineer",
            url="https://acme.wd5.myworkdayjobs.com/External/job/Remote/Software-Engineer_R-12345",
            description="<p>Do stuff</p>",
            location="Remote",
            posted_at="Posted 3 Days Ago",
        )
    ]


def test_list_postings_external_id_falls_back_to_external_path(monkeypatch, sleeps):
    route_post(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "total": 1,
                    "jobPostings": [
                        {
                            "title": "Engineer",
                            "externalPath": "/job/Remote/Software-Engineer",
                            "locationsText": "Remote",
                            "postedOn": "Posted Today",
                        }
                    ],
                }
            )
        ),
    )
    route_get(monkeypatch, sequence(FakeResponse(json_data={"jobPostingInfo": {}})))
    postings = workday.adapter.list_postings("acme|wd5|External")
    assert postings[0].external_id == "/job/Remote/Software-Engineer"
    assert postings[0].description == ""


def test_list_postings_pages_by_offset(monkeypatch, sleeps):
    route_post(
        monkeypatch,
        sequence(
            FakeResponse(
                json_data={
                    "total": 25,
                    "jobPostings": [
                        {
                            "title": "Engineer 1",
                            "externalPath": "/job/A/Engineer-1_R-1",
                            "locationsText": "Remote",
                            "postedOn": "Posted Today",
                        }
                    ],
                }
            ),
            FakeResponse(
                json_data={
                    "total": 25,
                    "jobPostings": [
                        {
                            "title": "Engineer 2",
                            "externalPath": "/job/B/Engineer-2_R-2",
                            "locationsText": "Remote",
                            "postedOn": "Posted Today",
                        }
                    ],
                }
            ),
        ),
    )
    route_get(
        monkeypatch,
        sequence(
            FakeResponse(json_data={"jobPostingInfo": {"jobDescription": "d1"}}),
            FakeResponse(json_data={"jobPostingInfo": {"jobDescription": "d2"}}),
        ),
    )
    postings = workday.adapter.list_postings("acme|wd5|External")
    assert [p.external_id for p in postings] == ["R-1", "R-2"]
    assert [p.description for p in postings] == ["d1", "d2"]


def test_list_postings_returns_empty_on_404(monkeypatch, sleeps):
    route_post(monkeypatch, sequence(FakeResponse(status_code=404)))
    assert workday.adapter.list_postings("acme|wd5|External") == []


def test_list_postings_returns_empty_on_malformed_board_id(monkeypatch, sleeps):
    assert workday.adapter.list_postings("acme|wd5") == []


def test_list_postings_raises_on_unavailable(monkeypatch, sleeps):
    route_post(monkeypatch, sequence(*([FakeResponse(status_code=503)] * workday._MAX_ATTEMPTS)))
    with pytest.raises(RuntimeError):
        workday.adapter.list_postings("acme|wd5|External")
