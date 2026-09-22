"""
Unit tests for pipeline.jobs.ats.personio and pipeline.jobs.ats.jsonld (match_url/probe/
list_postings).

No network: each module's own requests.get is stubbed (both route entirely through their
module-local _fetch_text(), never pipeline.jobs.ats.fetch_json(), since Personio's feed is XML and
a careers page's body is raw HTML, not JSON), following tests/test_ats_greenhouse_lever.py's
route()/sequence() convention. Each module's own _sleep is stubbed the same way so
unavailable-status retries never actually wait.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.jobs.ats import jsonld, personio  # noqa: E402


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


@pytest.fixture
def sleeps_personio(monkeypatch):
    calls = []
    monkeypatch.setattr(personio, "_sleep", calls.append)
    return calls


@pytest.fixture
def sleeps_jsonld(monkeypatch):
    calls = []
    monkeypatch.setattr(jsonld, "_sleep", calls.append)
    return calls


def route(monkeypatch, module, handler):
    """Stub module.requests.get with handler(url, headers) -> FakeResponse; returns calls."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}})
        return handler(url, headers or {})

    monkeypatch.setattr(module.requests, "get", fake_get)
    return calls


def sequence(*responses):
    """A handler that returns each response in order, raising if called more times than given."""
    remaining = list(responses)

    def handler(url, headers):
        return remaining.pop(0)

    return handler


# --- personio: match_url -----------------------------------------------------------------

PERSONIO_URLS = [
    "https://acme.jobs.personio.de/job/123",
    "https://acme.jobs.personio.com/job/123?query=1",
]


@pytest.mark.parametrize("url", PERSONIO_URLS)
def test_personio_match_url_extracts_company(url):
    assert personio.adapter.match_url(url) == "acme"


def test_personio_match_url_no_match_returns_none():
    assert personio.adapter.match_url("https://boards.greenhouse.io/acme") is None
    assert personio.adapter.match_url("https://example.com/careers") is None


# --- personio: probe ----------------------------------------------------------------------

_FEED_WITH_POSITION = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>123</id>
    <name>Software Engineer</name>
    <office>Berlin</office>
    <department>Engineering</department>
    <createdAt>2026-09-01</createdAt>
    <url>https://acme.jobs.personio.de/job/123</url>
    <jobDescriptions>
      <jobDescription>
        <name>Intro</name>
        <value>Join our team.</value>
      </jobDescription>
      <jobDescription>
        <name>Requirements</name>
        <value>5 years experience.</value>
      </jobDescription>
    </jobDescriptions>
  </position>
</workzag-jobs>"""

_EMPTY_FEED = '<?xml version="1.0" encoding="UTF-8"?>\n<workzag-jobs></workzag-jobs>'

_FEED_NO_URL = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>456</id>
    <name>Designer</name>
    <city>Munich</city>
    <createdAt>2026-09-02</createdAt>
  </position>
</workzag-jobs>"""


def test_personio_probe_returns_board_info_on_valid_feed(monkeypatch, sleeps_personio):
    route(monkeypatch, personio, sequence(FakeResponse(text=_FEED_WITH_POSITION)))
    assert personio.adapter.probe("acme") == personio.BoardInfo(board_id="acme")


def test_personio_probe_returns_board_info_on_empty_feed(monkeypatch, sleeps_personio):
    route(monkeypatch, personio, sequence(FakeResponse(text=_EMPTY_FEED)))
    assert personio.adapter.probe("acme") == personio.BoardInfo(board_id="acme")


def test_personio_probe_returns_none_on_404(monkeypatch, sleeps_personio):
    route(monkeypatch, personio, sequence(FakeResponse(status_code=404)))
    assert personio.adapter.probe("nope") is None


def test_personio_probe_returns_none_on_malformed_xml(monkeypatch, sleeps_personio):
    route(monkeypatch, personio, sequence(FakeResponse(text="not xml at all <<<")))
    assert personio.adapter.probe("acme") is None


def test_personio_probe_raises_on_unavailable(monkeypatch, sleeps_personio):
    route(
        monkeypatch,
        personio,
        sequence(*([FakeResponse(status_code=503)] * personio._MAX_ATTEMPTS)),
    )
    with pytest.raises(RuntimeError):
        personio.adapter.probe("acme")


# --- personio: list_postings ----------------------------------------------------------------


def test_personio_list_postings_maps_fields(monkeypatch, sleeps_personio):
    route(monkeypatch, personio, sequence(FakeResponse(text=_FEED_WITH_POSITION)))
    postings = personio.adapter.list_postings("acme")
    assert postings == [
        personio.RawPosting(
            external_id="123",
            title="Software Engineer",
            url="https://acme.jobs.personio.de/job/123",
            description="Join our team.\n\n5 years experience.",
            location="Berlin",
            posted_at="2026-09-01",
        )
    ]


def test_personio_list_postings_constructs_url_when_missing(monkeypatch, sleeps_personio):
    route(monkeypatch, personio, sequence(FakeResponse(text=_FEED_NO_URL)))
    postings = personio.adapter.list_postings("acme")
    assert postings == [
        personio.RawPosting(
            external_id="456",
            title="Designer",
            url="https://acme.jobs.personio.de/job/456",
            description="",
            location="Munich",
            posted_at="2026-09-02",
        )
    ]


def test_personio_list_postings_returns_empty_on_empty_feed(monkeypatch, sleeps_personio):
    route(monkeypatch, personio, sequence(FakeResponse(text=_EMPTY_FEED)))
    assert personio.adapter.list_postings("acme") == []


def test_personio_list_postings_returns_empty_on_404(monkeypatch, sleeps_personio):
    route(monkeypatch, personio, sequence(FakeResponse(status_code=404)))
    assert personio.adapter.list_postings("nope") == []


def test_personio_list_postings_returns_empty_on_malformed_xml(monkeypatch, sleeps_personio):
    route(monkeypatch, personio, sequence(FakeResponse(text="not xml at all <<<")))
    assert personio.adapter.list_postings("acme") == []


def test_personio_list_postings_raises_on_unavailable(monkeypatch, sleeps_personio):
    route(
        monkeypatch,
        personio,
        sequence(*([FakeResponse(status_code=503)] * personio._MAX_ATTEMPTS)),
    )
    with pytest.raises(RuntimeError):
        personio.adapter.list_postings("acme")


# --- jsonld: match_url ----------------------------------------------------------------------


def test_jsonld_match_url_always_none():
    assert jsonld.adapter.match_url("https://example.com/careers") is None
    assert jsonld.adapter.match_url("https://boards.greenhouse.io/acme") is None


# --- jsonld: probe --------------------------------------------------------------------------

_SINGLE_JOBPOSTING_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"JobPosting","title":"Engineer",
"url":"https://example.com/jobs/1","description":"Do stuff",
"identifier":{"@type":"PropertyValue","name":"acme","value":"job-1"},
"datePosted":"2026-09-01",
"jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
"addressLocality":"Berlin","addressRegion":"BE"}}}
</script>
</head><body></body></html>"""

_NO_JOBPOSTING_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"Organization","name":"Acme"}
</script>
</head><body></body></html>"""

_GRAPH_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org/","@graph":[
{"@type":"JobPosting","title":"Engineer","url":"https://example.com/jobs/1"},
{"@type":"JobPosting","title":"Designer","url":"https://example.com/jobs/2"}
]}
</script>
</head></html>"""

_NO_IDENTIFIER_HTML = """<html><head>
<script type="application/ld+json">
{"@type":"JobPosting","title":"Engineer","url":"https://example.com/jobs/3"}
</script>
</head></html>"""


def test_jsonld_probe_returns_board_info_when_jobposting_present(monkeypatch, sleeps_jsonld):
    route(monkeypatch, jsonld, sequence(FakeResponse(text=_SINGLE_JOBPOSTING_HTML)))
    info = jsonld.adapter.probe("https://example.com/careers")
    assert info == jsonld.BoardInfo(board_id="https://example.com/careers", confidence=0.6)


def test_jsonld_probe_returns_none_when_no_jobposting(monkeypatch, sleeps_jsonld):
    route(monkeypatch, jsonld, sequence(FakeResponse(text=_NO_JOBPOSTING_HTML)))
    assert jsonld.adapter.probe("https://example.com/careers") is None


def test_jsonld_probe_returns_none_on_404(monkeypatch, sleeps_jsonld):
    route(monkeypatch, jsonld, sequence(FakeResponse(status_code=404)))
    assert jsonld.adapter.probe("https://example.com/careers") is None


def test_jsonld_probe_raises_on_unavailable(monkeypatch, sleeps_jsonld):
    route(
        monkeypatch,
        jsonld,
        sequence(*([FakeResponse(status_code=503)] * jsonld._MAX_ATTEMPTS)),
    )
    with pytest.raises(RuntimeError):
        jsonld.adapter.probe("https://example.com/careers")


# --- jsonld: list_postings ------------------------------------------------------------------


def test_jsonld_list_postings_single_jobposting(monkeypatch, sleeps_jsonld):
    route(monkeypatch, jsonld, sequence(FakeResponse(text=_SINGLE_JOBPOSTING_HTML)))
    postings = jsonld.adapter.list_postings("https://example.com/careers")
    assert postings == [
        jsonld.RawPosting(
            external_id="job-1",
            title="Engineer",
            url="https://example.com/jobs/1",
            description="Do stuff",
            location="Berlin, BE",
            posted_at="2026-09-01",
        )
    ]


def test_jsonld_list_postings_graph_multiple(monkeypatch, sleeps_jsonld):
    route(monkeypatch, jsonld, sequence(FakeResponse(text=_GRAPH_HTML)))
    postings = jsonld.adapter.list_postings("https://example.com/careers")
    assert [p.title for p in postings] == ["Engineer", "Designer"]
    assert [p.url for p in postings] == [
        "https://example.com/jobs/1",
        "https://example.com/jobs/2",
    ]


def test_jsonld_list_postings_fallback_external_id_hash(monkeypatch, sleeps_jsonld):
    route(monkeypatch, jsonld, sequence(FakeResponse(text=_NO_IDENTIFIER_HTML)))
    postings = jsonld.adapter.list_postings("https://example.com/careers")
    expected_id = hashlib.sha1(b"Engineer|https://example.com/jobs/3").hexdigest()
    assert postings[0].external_id == expected_id


def test_jsonld_list_postings_returns_empty_on_no_jobposting(monkeypatch, sleeps_jsonld):
    route(monkeypatch, jsonld, sequence(FakeResponse(text=_NO_JOBPOSTING_HTML)))
    assert jsonld.adapter.list_postings("https://example.com/careers") == []


def test_jsonld_list_postings_returns_empty_on_404(monkeypatch, sleeps_jsonld):
    route(monkeypatch, jsonld, sequence(FakeResponse(status_code=404)))
    assert jsonld.adapter.list_postings("https://example.com/careers") == []


def test_jsonld_list_postings_raises_on_unavailable(monkeypatch, sleeps_jsonld):
    route(
        monkeypatch,
        jsonld,
        sequence(*([FakeResponse(status_code=503)] * jsonld._MAX_ATTEMPTS)),
    )
    with pytest.raises(RuntimeError):
        jsonld.adapter.list_postings("https://example.com/careers")
