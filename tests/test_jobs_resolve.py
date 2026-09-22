"""
Unit tests for pipeline.jobs.resolve (ResolveResult, resolve_company's ladder, probe_slugs,
fingerprint_html).

No network: requests.get is stubbed at the module's own import site
(monkeypatch.setattr(resolve.requests, "get", fake), matching tests/test_jobs_ats_base.py's
convention). ATS adapters are stubbed via monkeypatch.setattr(resolve, "get_adapter", fake) /
monkeypatch.setattr(resolve, "match_any_url", fake) rather than touching pipeline.jobs.ats.* or
any real adapter module. Where a test only needs match_url()'s pure regex matching (no network),
the real pipeline.jobs.ats.match_any_url() is used unmocked against a real ATS-shaped URL
(e.g. boards.greenhouse.io) for a more integration-like check.
"""
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import JobCompany  # noqa: E402
from pipeline.jobs import resolve  # noqa: E402
from pipeline.jobs.ats import BoardInfo  # noqa: E402


def _company(name="Acme Inc", domain=None):
    return JobCompany(name=name, domain=domain, source="watchlist")


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


def route(monkeypatch, handler):
    """Stub resolve.requests.get with handler(url) -> FakeResponse (or raises)."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return handler(url)

    monkeypatch.setattr(resolve.requests, "get", fake_get)
    return calls


class FakeAdapter:
    """A stand-in AtsAdapter whose probe() returns a scripted BoardInfo/None per slug, or raises
    a scripted exception."""

    def __init__(self, responses=None, raises=None):
        self.responses = responses or {}
        self.raises = raises
        self.calls = []

    def probe(self, slug):
        self.calls.append(slug)
        if self.raises:
            raise self.raises
        return self.responses.get(slug)


# --- ResolveResult -----------------------------------------------------------------------------


def test_resolve_result_fields():
    result = resolve.ResolveResult(
        ats_kind="greenhouse", board_id="acme", resolved_by="pattern", confidence=1.0
    )
    assert result.ats_kind == "greenhouse"
    assert result.board_id == "acme"
    assert result.resolved_by == "pattern"
    assert result.confidence == 1.0


# --- resolve_company: step 1, URL pattern -------------------------------------------------------


def test_resolve_company_pattern_hit_short_circuits(monkeypatch):
    monkeypatch.setattr(resolve, "match_any_url", lambda url: ("greenhouse", "acme"))

    def fail_probe_slugs(company):
        raise AssertionError("probe_slugs should not run when a pattern hit is found")

    monkeypatch.setattr(resolve, "probe_slugs", fail_probe_slugs)

    result = resolve.resolve_company(_company(), known_urls=["https://boards.greenhouse.io/acme"])
    assert result == resolve.ResolveResult(
        ats_kind="greenhouse", board_id="acme", resolved_by="pattern", confidence=1.0
    )


def test_resolve_company_pattern_miss_falls_through(monkeypatch):
    monkeypatch.setattr(resolve, "match_any_url", lambda url: None)
    monkeypatch.setattr(resolve, "probe_slugs", lambda company: None)
    monkeypatch.setattr(resolve, "_fingerprint", lambda company: (None, None))

    result = resolve.resolve_company(_company(), known_urls=["https://example.com/careers"])
    assert result is None


def test_resolve_company_no_known_urls_skips_pattern_step(monkeypatch):
    calls = []
    monkeypatch.setattr(resolve, "match_any_url", lambda url: calls.append(url))
    monkeypatch.setattr(resolve, "probe_slugs", lambda company: None)
    monkeypatch.setattr(resolve, "_fingerprint", lambda company: (None, None))

    result = resolve.resolve_company(_company())
    assert calls == []
    assert result is None


# --- probe_slugs ---------------------------------------------------------------------------------


def test_probe_slugs_no_candidates_returns_none(monkeypatch):
    monkeypatch.setattr(resolve, "slug_candidates", lambda name, domain: [])
    assert resolve.probe_slugs(_company()) is None


def test_probe_slugs_accepts_name_confirmed_hit(monkeypatch):
    fake = FakeAdapter(responses={"acme": BoardInfo(board_id="acme", company_name="Acme Inc")})
    monkeypatch.setattr(resolve, "slug_candidates", lambda name, domain: ["acme"])
    monkeypatch.setattr(resolve, "get_adapter", lambda kind: fake)

    result = resolve.probe_slugs(_company(name="Acme Inc"))
    assert result.resolved_by == "slug_probe"
    assert result.board_id == "acme"
    assert result.confidence == resolve._SLUG_PROBE_CONFIDENCE_NAME_CONFIRMED
    assert result.ats_kind == resolve._SLUG_PROBE_KINDS[0]


def test_probe_slugs_accepts_unconfirmed_hit_at_lower_confidence(monkeypatch):
    fake = FakeAdapter(responses={"acme": BoardInfo(board_id="acme", company_name=None)})
    monkeypatch.setattr(resolve, "slug_candidates", lambda name, domain: ["acme"])
    monkeypatch.setattr(resolve, "get_adapter", lambda kind: fake)

    result = resolve.probe_slugs(_company(name="Acme Inc"))
    assert result.confidence == resolve._SLUG_PROBE_CONFIDENCE_UNCONFIRMED
    assert result.confidence < resolve._SLUG_PROBE_CONFIDENCE_NAME_CONFIRMED


def test_probe_slugs_rejects_name_mismatch_as_false_positive(monkeypatch):
    # A generic slug ("engineering") that happens to exist on some unrelated company's board must
    # not be accepted just because the ATS returned a 2xx — the research doc's false-positive risk.
    fake = FakeAdapter(
        responses={"engineering": BoardInfo(board_id="engineering", company_name="Totally Unrelated Corp")}
    )
    monkeypatch.setattr(resolve, "slug_candidates", lambda name, domain: ["engineering"])
    monkeypatch.setattr(resolve, "get_adapter", lambda kind: fake)

    result = resolve.probe_slugs(_company(name="Acme Inc"))
    assert result is None


def test_probe_slugs_swallows_runtime_error_and_keeps_trying(monkeypatch):
    flaky = FakeAdapter(raises=RuntimeError("unavailable"))
    healthy = FakeAdapter(responses={"acme": BoardInfo(board_id="acme", company_name=None)})

    def fake_get_adapter(kind):
        return flaky if kind == resolve._SLUG_PROBE_KINDS[0] else healthy

    monkeypatch.setattr(resolve, "slug_candidates", lambda name, domain: ["acme"])
    monkeypatch.setattr(resolve, "get_adapter", fake_get_adapter)

    result = resolve.probe_slugs(_company(name="Acme Inc"))
    assert result is not None
    assert result.ats_kind == resolve._SLUG_PROBE_KINDS[1]


def test_probe_slugs_caps_total_attempts(monkeypatch):
    fake = FakeAdapter(responses={})  # every probe misses
    monkeypatch.setattr(resolve, "slug_candidates", lambda name, domain: ["a", "b", "c"])
    monkeypatch.setattr(resolve, "get_adapter", lambda kind: fake)

    result = resolve.probe_slugs(_company())
    assert result is None
    assert len(fake.calls) <= resolve._MAX_SLUG_PROBES


def test_probe_slugs_returns_none_when_every_candidate_misses(monkeypatch):
    fake = FakeAdapter(responses={})
    monkeypatch.setattr(resolve, "slug_candidates", lambda name, domain: ["acme"])
    monkeypatch.setattr(resolve, "get_adapter", lambda kind: fake)

    assert resolve.probe_slugs(_company()) is None


# --- fingerprint_html ------------------------------------------------------------------------


def test_fingerprint_html_no_domain_returns_none():
    assert resolve.fingerprint_html(_company(domain=None)) is None


def test_fingerprint_html_request_exception_returns_none(monkeypatch):
    def raiser(url):
        raise requests.ConnectionError("boom")

    route(monkeypatch, raiser)
    assert resolve.fingerprint_html(_company(domain="acme.com")) is None


def test_fingerprint_html_non_2xx_returns_none(monkeypatch):
    route(monkeypatch, lambda url: FakeResponse(status_code=404))
    assert resolve.fingerprint_html(_company(domain="acme.com")) is None


def test_fingerprint_html_finds_ats_link_on_homepage(monkeypatch):
    html = '<html><body><a href="https://boards.greenhouse.io/acme">Careers</a></body></html>'
    route(monkeypatch, lambda url: FakeResponse(text=html))

    result = resolve.fingerprint_html(_company(domain="acme.com"))
    assert result == resolve.ResolveResult(
        ats_kind="greenhouse",
        board_id="acme",
        resolved_by="html",
        confidence=resolve._HTML_FINGERPRINT_CONFIDENCE,
    )


def test_fingerprint_html_follows_careers_link_one_hop(monkeypatch):
    homepage_html = '<html><body><a href="https://acme.com/careers">Careers</a></body></html>'
    careers_html = '<html><body><a href="https://jobs.lever.co/acme">Apply</a></body></html>'

    def handler(url):
        if url == "https://acme.com/":
            return FakeResponse(text=homepage_html)
        if url == "https://acme.com/careers":
            return FakeResponse(text=careers_html)
        raise AssertionError(f"unexpected fetch: {url}")

    route(monkeypatch, handler)
    result = resolve.fingerprint_html(_company(domain="acme.com"))
    assert result == resolve.ResolveResult(
        ats_kind="lever",
        board_id="acme",
        resolved_by="html",
        confidence=resolve._HTML_FINGERPRINT_CONFIDENCE,
    )


def test_fingerprint_html_no_careers_link_and_no_ats_link_returns_none(monkeypatch):
    html = "<html><body><a href=\"https://acme.com/about\">About</a></body></html>"
    route(monkeypatch, lambda url: FakeResponse(text=html))
    assert resolve.fingerprint_html(_company(domain="acme.com")) is None


def test_fingerprint_html_does_not_recurse_past_one_hop(monkeypatch):
    homepage_html = '<html><body><a href="https://acme.com/careers">Careers</a></body></html>'
    careers_html = '<html><body><a href="https://acme.com/careers/team">More jobs</a></body></html>'
    calls = []

    def handler(url):
        calls.append(url)
        if url == "https://acme.com/":
            return FakeResponse(text=homepage_html)
        if url == "https://acme.com/careers":
            return FakeResponse(text=careers_html)
        raise AssertionError(f"unexpected second-hop fetch: {url}")

    route(monkeypatch, handler)
    result = resolve.fingerprint_html(_company(domain="acme.com"))
    assert result is None
    assert calls == ["https://acme.com/", "https://acme.com/careers"]


# --- resolve_company: step 4, JSON-LD fallback ---------------------------------------------------


def test_resolve_company_jsonld_fallback_reuses_fingerprint_careers_url(monkeypatch):
    monkeypatch.setattr(resolve, "match_any_url", lambda url: None)
    monkeypatch.setattr(resolve, "probe_slugs", lambda company: None)
    monkeypatch.setattr(
        resolve, "_fingerprint", lambda company: (None, "https://acme.com/careers")
    )

    class FakeJsonLd:
        def probe(self, url):
            assert url == "https://acme.com/careers"
            return BoardInfo(board_id=url, confidence=0.6)

    monkeypatch.setattr(resolve, "get_adapter", lambda kind: FakeJsonLd())

    result = resolve.resolve_company(_company(domain="acme.com"))
    assert result == resolve.ResolveResult(
        ats_kind="jsonld", board_id="https://acme.com/careers", resolved_by="jsonld", confidence=0.6
    )


def test_resolve_company_jsonld_fallback_guesses_careers_url_when_none_fetched(monkeypatch):
    monkeypatch.setattr(resolve, "match_any_url", lambda url: None)
    monkeypatch.setattr(resolve, "probe_slugs", lambda company: None)
    monkeypatch.setattr(resolve, "_fingerprint", lambda company: (None, None))

    class FakeJsonLd:
        def probe(self, url):
            assert url == "https://acme.com/careers"
            return BoardInfo(board_id=url, confidence=0.6)

    monkeypatch.setattr(resolve, "get_adapter", lambda kind: FakeJsonLd())

    result = resolve.resolve_company(_company(domain="acme.com"))
    assert result is not None
    assert result.resolved_by == "jsonld"


def test_resolve_company_jsonld_miss_returns_none(monkeypatch):
    monkeypatch.setattr(resolve, "match_any_url", lambda url: None)
    monkeypatch.setattr(resolve, "probe_slugs", lambda company: None)
    monkeypatch.setattr(resolve, "_fingerprint", lambda company: (None, None))

    class FakeJsonLd:
        def probe(self, url):
            return None

    monkeypatch.setattr(resolve, "get_adapter", lambda kind: FakeJsonLd())

    assert resolve.resolve_company(_company(domain="acme.com")) is None


def test_resolve_company_no_domain_skips_jsonld_fallback(monkeypatch):
    monkeypatch.setattr(resolve, "match_any_url", lambda url: None)
    monkeypatch.setattr(resolve, "probe_slugs", lambda company: None)
    monkeypatch.setattr(resolve, "_fingerprint", lambda company: (None, None))

    def fail_get_adapter(kind):
        raise AssertionError("jsonld should not be probed with no domain and no careers_url")

    monkeypatch.setattr(resolve, "get_adapter", fail_get_adapter)

    assert resolve.resolve_company(_company(domain=None)) is None


def test_resolve_company_jsonld_runtime_error_returns_none(monkeypatch):
    monkeypatch.setattr(resolve, "match_any_url", lambda url: None)
    monkeypatch.setattr(resolve, "probe_slugs", lambda company: None)
    monkeypatch.setattr(resolve, "_fingerprint", lambda company: (None, None))

    class FakeJsonLd:
        def probe(self, url):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(resolve, "get_adapter", lambda kind: FakeJsonLd())

    assert resolve.resolve_company(_company(domain="acme.com")) is None


# --- resolve_company: ladder ordering end-to-end -------------------------------------------------


def test_resolve_company_slug_probe_wins_over_html(monkeypatch):
    monkeypatch.setattr(resolve, "match_any_url", lambda url: None)
    monkeypatch.setattr(
        resolve,
        "probe_slugs",
        lambda company: resolve.ResolveResult(
            ats_kind="greenhouse", board_id="acme", resolved_by="slug_probe", confidence=0.9
        ),
    )

    def fail_fingerprint(company):
        raise AssertionError("fingerprint should not run once slug_probe already hit")

    monkeypatch.setattr(resolve, "_fingerprint", fail_fingerprint)

    result = resolve.resolve_company(_company(domain="acme.com"))
    assert result.resolved_by == "slug_probe"
