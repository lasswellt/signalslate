"""
Unit tests for pipeline.domains.ideas (generate_candidates, llm_ideas, prefilter).

No network: llm_ideas()'s Anthropic client (a true external) is a scripted FakeClient, following
tests/test_triage.py's style; prefilter()'s DNS resolver and RDAP domain_lookup/bootstrap are the
same fake-object style as tests/test_domain_dns.py and tests/test_domain_rdap.py. Nothing under
pipeline/ is mocked.
"""
import sys
from pathlib import Path
from typing import Any, cast
from types import SimpleNamespace

import anthropic
import dns.exception
import dns.resolver
import pytest
from whoisit.errors import ResourceDoesNotExist, UnsupportedError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.domains import ideas  # noqa: E402
from pipeline.triage import TriageConfigError  # noqa: E402


# --- generate_candidates -----------------------------------------------------------------------


def test_generate_candidates_basic_combinations():
    result = ideas.generate_candidates(["a", "b"], ["com", "net"], affixes=["get", "my"], allow_hyphen=True)
    # seed-major, affix-minor, tld-innermost, deterministic order
    assert result[0] == "a.com"
    assert result[1] == "a.net"
    assert "geta.com" in result
    assert "aget.com" in result
    assert "get-a.com" in result
    assert "a-get.com" in result
    assert "b.com" in result
    assert len(result) == len(set(result))  # no duplicates


def test_generate_candidates_no_affixes_or_hyphen_by_default():
    result = ideas.generate_candidates(["seed"], ["com"])
    assert result == ["seed.com"]


def test_generate_candidates_dedupes_across_combinations():
    # "my" + "seed" and "seed" + "my" differ, but the same seed x tld with itself as an affix can
    # legitimately collide once normalized; generate_candidates must keep only the first.
    result = ideas.generate_candidates(["seed"], ["com"], affixes=["seed"])
    assert result.count("seedseed.com") == 1


def test_generate_candidates_drops_invalid_after_normalize():
    # a "/" makes normalize_domain() treat the candidate as a path, not a domain
    result = ideas.generate_candidates(["a/b", "ok"], ["com"])
    assert result == ["ok.com"]


def test_generate_candidates_respects_max_len():
    result = ideas.generate_candidates(["short", "waytoolongseed"], ["com"], max_len=10)
    assert result == ["short.com"]


def test_generate_candidates_strips_leading_dot_and_case_on_tld():
    result = ideas.generate_candidates(["Seed"], [".COM"])
    assert result == ["seed.com"]


def test_generate_candidates_empty_inputs_yield_empty_list():
    assert ideas.generate_candidates([], ["com"]) == []
    assert ideas.generate_candidates(["seed"], []) == []


def test_generate_candidates_is_capped_at_500():
    seeds = [f"seed{n}" for n in range(600)]
    result = ideas.generate_candidates(seeds, ["com"])
    assert len(result) == 500
    assert result[0] == "seed0.com"
    assert result[-1] == "seed499.com"


# --- llm_ideas -----------------------------------------------------------------------------------


class FakeClient:
    """Scripted stand-in for anthropic.Anthropic: each parse() pops the next response or raises it."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.messages = SimpleNamespace(parse=self._parse)
        self.calls: list[dict] = []

    def _parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=cast(Any, SimpleNamespace(method="POST", url="https://example.com/v1/messages"))
    )


def idea_reply(*names: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    parsed = ideas._IdeaBatch(ideas=[ideas._IdeaRecord(name=name) for name in names])
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


def test_llm_ideas_normalizes_dedupes_and_caps():
    client = FakeClient(idea_reply("Foo.COM", "bar.net", "foo.com", "not-a-domain"))
    names, reason = ideas.llm_ideas("a todo app", n=2, client=client, model="test-model")
    assert reason == ""
    assert names == ["foo.com", "bar.net"]
    assert client.calls[0]["model"] == "test-model"
    assert client.calls[0]["output_format"] is ideas._IdeaBatch


def test_llm_ideas_success_with_zero_usable_names_is_not_a_failure():
    client = FakeClient(idea_reply("not-a-domain", "also bad"))
    names, reason = ideas.llm_ideas("a todo app", client=client, model="test-model")
    assert names == []
    assert reason == ""


def test_llm_ideas_refusal_degrades_to_empty_with_reason():
    client = FakeClient(idea_reply(stop_reason="refusal"))
    names, reason = ideas.llm_ideas("a todo app", client=client, model="test-model")
    assert names == []
    assert reason == "stop_reason=refusal"


def test_llm_ideas_no_parsed_output_degrades_to_empty_with_reason():
    client = FakeClient(SimpleNamespace(parsed_output=None, stop_reason="end_turn"))
    names, reason = ideas.llm_ideas("a todo app", client=client, model="test-model")
    assert names == []
    assert reason == "no parsed output"


def test_llm_ideas_api_error_degrades_to_empty_with_class_name_reason():
    client = FakeClient(connection_error())
    names, reason = ideas.llm_ideas("a todo app", client=client, model="test-model")
    assert names == []
    assert reason == "APIConnectionError"


def test_llm_ideas_unconfigured_never_raises(monkeypatch: pytest.MonkeyPatch):
    def fake_make_client() -> Any:
        raise TriageConfigError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(ideas, "make_client", fake_make_client)
    names, reason = ideas.llm_ideas("a todo app")
    assert names == []
    assert reason == "ANTHROPIC_API_KEY is not set"


def test_llm_ideas_blank_brief_raises_value_error():
    with pytest.raises(ValueError):
        ideas.llm_ideas("   ", client=FakeClient())


def test_llm_ideas_non_positive_n_raises_value_error():
    with pytest.raises(ValueError):
        ideas.llm_ideas("a todo app", n=0, client=FakeClient())


# --- prefilter -----------------------------------------------------------------------------------


class FakeRdata:
    def __init__(self, text: str):
        self._text = text

    def to_text(self) -> str:
        return self._text


class FakeResolver:
    """records maps (qname, rdtype) -> list[str] (answer texts), [] (NoAnswer), or an Exception
    instance to raise. A qname/rdtype pair absent from records raises NXDOMAIN."""

    def __init__(self, records: dict):
        self.records = records

    def resolve(self, qname: str, rdtype: str, raise_on_no_answer: bool = True):
        key = (qname, rdtype)
        if key not in self.records:
            raise dns.resolver.NXDOMAIN()
        value = self.records[key]
        if isinstance(value, Exception):
            raise value
        if not value:
            raise dns.resolver.NoAnswer()
        return [FakeRdata(text) for text in value]


class FakeBootstrap:
    def __init__(self):
        self._bootstrapped = False

    def bootstrap(self, **kwargs):
        self._bootstrapped = True

    def is_bootstrapped(self):
        return self._bootstrapped


PARSED_OK = {
    "name": "TAKEN.COM",
    "nameservers": [],
    "status": [],
    "registration_date": None,
    "expiration_date": None,
    "entities": {},
}


def _rdap_kwargs(domain_lookup, boot: FakeBootstrap | None = None):
    boot = boot or FakeBootstrap()
    return {"domain_lookup": domain_lookup, "bootstrap": boot.bootstrap, "is_bootstrapped": boot.is_bootstrapped}


def test_prefilter_any_dns_record_is_taken():
    resolver = FakeResolver({("has-a.com", "A"): ["1.2.3.4"]})

    def domain_lookup(name):
        raise ResourceDoesNotExist("not found")

    result = ideas.prefilter(["has-a.com"], resolver=resolver, **_rdap_kwargs(domain_lookup))
    assert result == {"has-a.com": "taken"}


def test_prefilter_no_dns_and_rdap_not_found_is_likely_available():
    resolver = FakeResolver({})  # every type NXDOMAIN

    def domain_lookup(name):
        raise ResourceDoesNotExist("not found")

    result = ideas.prefilter(["free.com"], resolver=resolver, **_rdap_kwargs(domain_lookup))
    assert result == {"free.com": "likely_available"}


def test_prefilter_no_dns_but_rdap_ok_is_taken():
    resolver = FakeResolver({})

    def domain_lookup(name):
        return dict(PARSED_OK)

    result = ideas.prefilter(["taken.com"], resolver=resolver, **_rdap_kwargs(domain_lookup))
    assert result == {"taken.com": "taken"}


def test_prefilter_dns_lookup_failure_is_unknown_even_with_rdap_not_found():
    resolver = FakeResolver({("flaky.com", "A"): dns.exception.Timeout()})

    def domain_lookup(name):
        raise ResourceDoesNotExist("not found")

    result = ideas.prefilter(["flaky.com"], resolver=resolver, **_rdap_kwargs(domain_lookup))
    assert result == {"flaky.com": "unknown"}


def test_prefilter_unsupported_tld_is_unknown():
    resolver = FakeResolver({})

    def domain_lookup(name):
        raise UnsupportedError("no rdap for this tld")

    result = ideas.prefilter(["free.de"], resolver=resolver, **_rdap_kwargs(domain_lookup))
    assert result == {"free.de": "unknown"}


def test_prefilter_invalid_name_is_unknown_keyed_by_raw_input():
    result = ideas.prefilter(["not a domain!!"], resolver=FakeResolver({}), **_rdap_kwargs(lambda name: {}))
    assert result == {"not a domain!!": "unknown"}


def test_prefilter_never_calls_a_registrar_or_third_party_search():
    # prefilter's public surface is only dns/rdap injection points: nothing here can reach a
    # registrar API or a third-party domain-search site.
    assert set(ideas.prefilter.__code__.co_varnames[: ideas.prefilter.__code__.co_argcount]) <= {
        "names",
        "resolver",
        "domain_lookup",
        "bootstrap",
        "is_bootstrapped",
    }
