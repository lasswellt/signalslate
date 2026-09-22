"""
Unit tests for pipeline.jobs.research (llm_resolve, verify_llm_result, research_company).

No network: llm_resolve()'s Anthropic client (a true external) is a scripted FakeClient, following
tests/test_domain_ideas.py's style; verify_llm_result()'s ATS calls are stubbed via
monkeypatch.setattr(research, "get_adapter", ...) / monkeypatch.setattr(research, "match_any_url",
...), matching tests/test_jobs_resolve.py's convention. Nothing under pipeline/ is mocked.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.jobs import research  # noqa: E402
from pipeline.jobs.ats import BoardInfo  # noqa: E402
from pipeline.triage import TriageConfigError  # noqa: E402


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


def answer_reply(
    careers_url=None, ats_kind=None, board_id=None, confidence=0.5, stop_reason="end_turn"
) -> SimpleNamespace:
    parsed = research._ResearchAnswer(
        careers_url=careers_url, ats_kind=ats_kind, board_id=board_id, confidence=confidence
    )
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


# --- llm_resolve -----------------------------------------------------------------------------


def test_llm_resolve_success_returns_result_dict():
    client = FakeClient(
        answer_reply(ats_kind="greenhouse", board_id="acme", confidence=0.9)
    )
    result, reason = research.llm_resolve("Acme Inc", "acme.com", client=client, model="test-model")
    assert reason == ""
    assert result == {
        "careers_url": None,
        "ats_kind": "greenhouse",
        "board_id": "acme",
        "confidence": 0.9,
    }
    call = client.calls[0]
    assert call["model"] == "test-model"
    assert call["output_format"] is research._ResearchAnswer
    tool_types = {tool["type"] for tool in call["tools"]}
    assert "web_search_20260318" in tool_types
    assert "web_fetch_20260318" in tool_types
    for tool in call["tools"]:
        assert tool["max_uses"] == 3


def test_llm_resolve_passes_domain_and_company_name_in_payload():
    client = FakeClient(answer_reply())
    research.llm_resolve("Acme Inc", "acme.com", client=client, model="test-model")
    assert '"company_name": "Acme Inc"' in client.calls[0]["messages"][0]["content"]
    assert '"domain": "acme.com"' in client.calls[0]["messages"][0]["content"]


def test_llm_resolve_omits_domain_key_when_unset():
    client = FakeClient(answer_reply())
    research.llm_resolve("Acme Inc", client=client, model="test-model")
    assert '"domain"' not in client.calls[0]["messages"][0]["content"]


def test_llm_resolve_refusal_degrades_to_none_with_reason():
    client = FakeClient(answer_reply(stop_reason="refusal"))
    result, reason = research.llm_resolve("Acme Inc", client=client, model="test-model")
    assert result is None
    assert reason == "stop_reason=refusal"


def test_llm_resolve_no_parsed_output_degrades_to_none_with_reason():
    client = FakeClient(SimpleNamespace(parsed_output=None, stop_reason="end_turn"))
    result, reason = research.llm_resolve("Acme Inc", client=client, model="test-model")
    assert result is None
    assert reason == "no parsed output"


def test_llm_resolve_api_error_degrades_to_none_with_class_name_reason():
    client = FakeClient(connection_error())
    result, reason = research.llm_resolve("Acme Inc", client=client, model="test-model")
    assert result is None
    assert reason == "APIConnectionError"


def test_llm_resolve_unconfigured_never_raises(monkeypatch: pytest.MonkeyPatch):
    def fake_make_client() -> Any:
        raise TriageConfigError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(research, "make_client", fake_make_client)
    result, reason = research.llm_resolve("Acme Inc")
    assert result is None
    assert reason == "ANTHROPIC_API_KEY is not set"


# --- verify_llm_result -------------------------------------------------------------------------


class FakeAdapter:
    def __init__(self, responses=None, raises=None):
        self.responses = responses or {}
        self.raises = raises
        self.calls = []

    def probe(self, board_id):
        self.calls.append(board_id)
        if self.raises:
            raise self.raises
        return self.responses.get(board_id)


def test_verify_confirms_ats_kind_and_board_id(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter({"acme": BoardInfo(board_id="acme", company_name="Acme Inc", confidence=1.0)})
    calls = []

    def fake_get_adapter(kind):
        calls.append(kind)
        return adapter

    monkeypatch.setattr(research, "get_adapter", fake_get_adapter)
    result = research.verify_llm_result(
        {"ats_kind": "greenhouse", "board_id": "acme", "confidence": 0.95, "careers_url": None},
        "Acme Inc",
        "acme.com",
    )

    assert result is not None
    assert result.ats_kind == "greenhouse"
    assert result.board_id == "acme"
    assert result.resolved_by == "llm"
    # capped at _LLM_CONFIDENCE_CAP (0.8) even though both model (0.95) and adapter (1.0) are higher
    assert result.confidence == 0.8
    assert calls == ["greenhouse"]


def test_verify_uses_lower_adapter_confidence_when_below_cap(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter({"acme": BoardInfo(board_id="acme", confidence=0.6)})
    monkeypatch.setattr(research, "get_adapter", lambda kind: adapter)

    result = research.verify_llm_result(
        {"ats_kind": "greenhouse", "board_id": "acme", "confidence": 0.95}, "Acme Inc", None
    )
    assert result is not None
    assert result.confidence == 0.6


def test_verify_probe_none_is_unverified(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter({})
    monkeypatch.setattr(research, "get_adapter", lambda kind: adapter)

    result = research.verify_llm_result(
        {"ats_kind": "greenhouse", "board_id": "nope", "confidence": 0.9}, "Acme Inc", None
    )
    assert result is None
    assert adapter.calls == ["nope"]


def test_verify_bogus_ats_kind_returns_none(monkeypatch: pytest.MonkeyPatch):
    def fake_get_adapter(kind):
        raise ValueError(f"unknown ATS kind: {kind!r}")

    monkeypatch.setattr(research, "get_adapter", fake_get_adapter)

    result = research.verify_llm_result(
        {"ats_kind": "not-a-real-ats", "board_id": "acme", "confidence": 0.9}, "Acme Inc", None
    )
    assert result is None


def test_verify_unimplemented_ats_module_returns_none(monkeypatch: pytest.MonkeyPatch):
    def fake_get_adapter(kind):
        raise ModuleNotFoundError(f"no module named {kind!r}")

    monkeypatch.setattr(research, "get_adapter", fake_get_adapter)

    result = research.verify_llm_result(
        {"ats_kind": "workday", "board_id": "acme", "confidence": 0.9}, "Acme Inc", None
    )
    assert result is None


def test_verify_probe_runtime_error_returns_none(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter(raises=RuntimeError("unavailable"))
    monkeypatch.setattr(research, "get_adapter", lambda kind: adapter)

    result = research.verify_llm_result(
        {"ats_kind": "greenhouse", "board_id": "acme", "confidence": 0.9}, "Acme Inc", None
    )
    assert result is None


def test_verify_careers_url_matching_known_ats_is_confirmed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(research, "match_any_url", lambda url: ("greenhouse", "acme"))

    result = research.verify_llm_result(
        {"careers_url": "https://boards.greenhouse.io/acme", "ats_kind": None, "board_id": None, "confidence": 0.7},
        "Acme Inc",
        None,
    )
    assert result is not None
    assert result.ats_kind == "greenhouse"
    assert result.board_id == "acme"
    assert result.resolved_by == "llm"
    assert result.confidence == 0.7


def test_verify_careers_url_with_no_ats_match_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(research, "match_any_url", lambda url: None)

    result = research.verify_llm_result(
        {"careers_url": "https://acme.com/careers", "ats_kind": None, "board_id": None, "confidence": 0.7},
        "Acme Inc",
        None,
    )
    assert result is None


def test_verify_empty_result_returns_none():
    result = research.verify_llm_result(
        {"careers_url": None, "ats_kind": None, "board_id": None, "confidence": 0.0}, "Acme Inc", None
    )
    assert result is None


# --- research_company --------------------------------------------------------------------------


def test_research_company_success_end_to_end(monkeypatch: pytest.MonkeyPatch):
    client = FakeClient(answer_reply(ats_kind="lever", board_id="acme", confidence=0.9))
    adapter = FakeAdapter({"acme": BoardInfo(board_id="acme", confidence=1.0)})
    monkeypatch.setattr(research, "get_adapter", lambda kind: adapter)

    result, reason = research.research_company("Acme Inc", "acme.com", client=client, model="test-model")
    assert reason == ""
    assert result is not None
    assert result.ats_kind == "lever"
    assert result.board_id == "acme"
    assert result.resolved_by == "llm"


def test_research_company_llm_failure_passes_reason_through():
    client = FakeClient(connection_error())
    result, reason = research.research_company("Acme Inc", client=client, model="test-model")
    assert result is None
    assert reason == "APIConnectionError"


def test_research_company_unverified_claim_is_none_with_empty_reason(monkeypatch: pytest.MonkeyPatch):
    client = FakeClient(answer_reply(careers_url="https://acme.com/careers", confidence=0.5))
    monkeypatch.setattr(research, "match_any_url", lambda url: None)

    result, reason = research.research_company("Acme Inc", client=client, model="test-model")
    assert result is None
    assert reason == ""
