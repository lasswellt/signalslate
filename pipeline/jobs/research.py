"""
LLM-assisted careers-page research for pipeline/jobs/: resolver-ladder step 4 (research doc
docs/_research/2026-09-21_jobs-collector.md §Resolution step 4), for a company the deterministic
ladder (pipeline.jobs.resolve.resolve_company) could not resolve.

Design decisions:

- Reuses pipeline.triage.make_client()/pipeline.health.llm_settings() instead of wiring a second
  Anthropic client, the same convention pipeline.domains.ideas.llm_ideas() follows. A missing key
  (TriageConfigError from make_client()), a refusal, or any SDK/parse failure all degrade to
  (None, reason) rather than raising; reason never carries the exception message, only its class
  name (pipeline.triage/pipeline.domains.ideas convention — a message can echo request/response
  detail).
- llm_resolve() gives the model Anthropic's server-side web_search and web_fetch tools (this
  installed SDK's newest exported tool types — web_search_20260318 / web_fetch_20260318, confirmed
  against venv/lib/python3.12/site-packages/anthropic/types/__init__.py and the two param files),
  each capped at max_uses=3 per the research doc's budget, and asks for a structured _ResearchAnswer
  via output_format rather than free text.
- The model's answer is UNTRUSTED input, exactly like triage's map stage and ideas' llm_ideas():
  nothing it reports is stored on its say-so. verify_llm_result() independently re-checks it: an
  ats_kind + board_id pair must be confirmed by that ATS's own probe() (pipeline.jobs.ats.
  get_adapter), and a bare careers_url with no recognized ATS is re-checked against
  pipeline.jobs.ats.match_any_url() once more before being accepted — a careers URL the model found
  that matches no known ATS host is not, on its own, a storable resolution (T-015, not built yet,
  decides what if anything to do with that case; this module's job is only to produce a *verified*
  ResolveResult or nothing).
- verify_llm_result()'s stored confidence is never the model's own number taken verbatim: it is the
  min of the model's reported confidence, the confirming adapter's own BoardInfo.confidence, and a
  fixed _LLM_CONFIDENCE_CAP ceiling, so a model that claims confidence=1.0 can never outrank a
  directly-verified ladder step (pattern/slug_probe/html) in JobBoard.confidence, and an unconfirmed
  claim can never inflate the number a human reviewing it relies on.
- A bogus/hallucinated ats_kind (not one of pipeline.jobs.ats.ADAPTER_KINDS) or one whose module
  hasn't landed yet degrades to "unverified", not a crash: ValueError/ModuleNotFoundError from
  get_adapter() are both caught.
"""
import json
import logging
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

from pipeline.health import llm_settings
from pipeline.jobs.ats import get_adapter, match_any_url
from pipeline.jobs.resolve import ResolveResult
from pipeline.triage import TriageConfigError, make_client

_log = logging.getLogger(__name__)

_RESEARCH_MAX_TOKENS = 1024
# Research doc budget: "at most 3 web_search/web_fetch uses per company research call".
_TOOL_MAX_USES = 3
_MAX_CONTENT_TOKENS = 4096

# Ceiling applied to a verified LLM-sourced result regardless of the model's or the adapter's own
# reported confidence: resolved_by="llm" is inherently less certain than a directly-verified ladder
# step (pattern=1.0, slug_probe<=0.9, html=0.85), and this keeps it that way even if both inputs
# claim 1.0.
_LLM_CONFIDENCE_CAP = 0.8

_KNOWN_ATS_NAMES = (
    "Greenhouse", "Lever", "Ashby", "Workday", "SmartRecruiters", "Workable", "Rippling",
    "BambooHR", "Recruitee", "Personio",
)

_SYSTEM_PROMPT = f"""\
You research a company's careers/jobs page using web search and web fetch.

Given a company name (and optionally its domain), find the URL of its current careers or jobs \
listing page. If that page is hosted on one of these applicant tracking systems, report which one \
and the board's slug or tenant/token exactly as it appears in the URL: {", ".join(_KNOWN_ATS_NAMES)}. \
If it is hosted somewhere else, or you cannot tell which ATS it is, leave the ATS fields null and \
report only the careers URL. Report your confidence in the result as a number from 0 to 1. If you \
cannot find a careers page at all, return null for every field and a confidence of 0. Never invent \
a URL, slug or token you did not actually observe on a fetched page."""


class _ResearchAnswer(BaseModel):
    """The model's untrusted claim about a company's careers page. verify_llm_result() re-checks
    every field independently before anything here is ever stored."""

    model_config = ConfigDict(extra="forbid")

    careers_url: Optional[str]
    ats_kind: Optional[str]
    board_id: Optional[str]
    confidence: float


def _build_request(model: str, company_name: str, domain: Optional[str]) -> dict:
    """kwargs for client.messages.parse(): the user turn is a JSON string (same delimiting
    convention as pipeline.triage.build_request/pipeline.domains.ideas.llm_ideas), and the tools
    list gives the model web_search and web_fetch, each capped at _TOOL_MAX_USES."""
    payload: dict[str, Any] = {"company_name": company_name}
    if domain:
        payload["domain"] = domain
    return {
        "model": model,
        "max_tokens": _RESEARCH_MAX_TOKENS,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        "tools": [
            {"type": "web_search_20260318", "name": "web_search", "max_uses": _TOOL_MAX_USES},
            {
                "type": "web_fetch_20260318",
                "name": "web_fetch",
                "max_uses": _TOOL_MAX_USES,
                "max_content_tokens": _MAX_CONTENT_TOKENS,
            },
        ],
        "output_format": _ResearchAnswer,
    }


def llm_resolve(
    company_name: str,
    domain: Optional[str] = None,
    *,
    client: Optional[Any] = None,
    model: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    """
    Asks the model to find company_name's careers page via web_search/web_fetch tool use.

    Args:
        company_name: The company to research.
        domain: The company's known domain, when available; passed to the model as a hint.
        client: An anthropic.Anthropic-like object (duck-typed: messages.parse(**kwargs) returns
            an object with parsed_output/stop_reason). Tests inject a fake here instead of hitting
            the network. Defaults to pipeline.triage.make_client().
        model: Overrides llm_settings()["map_model"]; tests pass a fixed model id.

    Returns:
        (result, reason). result is a dict with keys careers_url/ats_kind/board_id/confidence
        (the model's UNTRUSTED claim — never store it without verify_llm_result()), or None when the
        call was skipped or failed. reason is "" on success, or a short, non-sensitive explanation
        (an unconfigured key, a refusal, or the failing exception's CLASS name, never its message)
        whenever result is None.

    Raises:
        Never raises: a configuration or model/API failure degrades to (None, reason), the same
        convention as pipeline.domains.ideas.llm_ideas().
    """
    active_client = client
    if active_client is None:
        try:
            active_client = make_client()
        except TriageConfigError as exc:
            return None, str(exc)

    active_model = model if model is not None else llm_settings()["map_model"]
    request = _build_request(active_model, company_name, domain)

    try:
        response = active_client.messages.parse(**request)
    except (anthropic.APIError, ValidationError) as exc:
        return None, type(exc).__name__
    except Exception as exc:
        # Class name only: an arbitrary exception here can echo the request or response text.
        return None, type(exc).__name__

    if response.stop_reason in ("refusal", "max_tokens"):
        return None, f"stop_reason={response.stop_reason}"
    if response.parsed_output is None:
        return None, "no parsed output"

    answer = response.parsed_output
    return (
        {
            "careers_url": answer.careers_url,
            "ats_kind": answer.ats_kind,
            "board_id": answer.board_id,
            "confidence": answer.confidence,
        },
        "",
    )


def _confidence(result: dict, *caps: float) -> float:
    """result's own confidence, clamped down by every value in caps (never up)."""
    raw = result.get("confidence")
    value = raw if isinstance(raw, (int, float)) else _LLM_CONFIDENCE_CAP
    return min(value, *caps)


def verify_llm_result(
    result: dict, company_name: str, domain: Optional[str]
) -> Optional[ResolveResult]:
    """
    Independently re-verifies llm_resolve()'s UNTRUSTED result before it can become a ResolveResult.

    Args:
        result: A dict shaped like llm_resolve()'s success return (careers_url/ats_kind/board_id/
            confidence) — the model's claim, never trusted on its own.
        company_name: The company being resolved. Accepted for interface symmetry with
            research_company() and future name-cross-checks; the check performed here is the
            adapter's own probe() confirmation, not a name match.
        domain: The company's known domain, when available. Accepted for interface symmetry;
            unused by the checks below (probe()/match_any_url() need only the reported board_id/
            careers_url).

    Returns:
        A ResolveResult(resolved_by="llm") only when independently confirmed:
          - result's ats_kind/board_id: confirmed by pipeline.jobs.ats.get_adapter(ats_kind).
            probe(board_id) actually returning a BoardInfo (not None). A bogus/hallucinated
            ats_kind (ValueError) or one whose adapter module hasn't landed (ModuleNotFoundError)
            is treated as unverified, not an error.
          - result's careers_url alone (no ats_kind/board_id): confirmed by
            pipeline.jobs.ats.match_any_url(careers_url) resolving to a known ATS host after all.
        None when neither check confirms anything — including when result carries neither an
        ats_kind/board_id pair nor a careers_url.

    Raises:
        Never raises: a probe() RuntimeError (transient network/5xx flake, matching
        pipeline.jobs.resolve.probe_slugs()'s own handling) is treated as unverified, not an error.
    """
    ats_kind = result.get("ats_kind")
    board_id = result.get("board_id")
    if ats_kind and board_id:
        try:
            adapter = get_adapter(ats_kind)
        except (ValueError, ModuleNotFoundError):
            return None
        try:
            info = adapter.probe(board_id)
        except RuntimeError:
            return None
        if info is None:
            return None
        return ResolveResult(
            ats_kind=ats_kind,
            board_id=info.board_id,
            resolved_by="llm",
            confidence=_confidence(result, info.confidence, _LLM_CONFIDENCE_CAP),
        )

    careers_url = result.get("careers_url")
    if careers_url:
        match = match_any_url(careers_url)
        if match:
            kind, matched_board_id = match
            return ResolveResult(
                ats_kind=kind,
                board_id=matched_board_id,
                resolved_by="llm",
                confidence=_confidence(result, _LLM_CONFIDENCE_CAP),
            )

    return None


def research_company(
    company_name: str,
    domain: Optional[str] = None,
    *,
    client: Optional[Any] = None,
    model: Optional[str] = None,
) -> tuple[Optional[ResolveResult], str]:
    """
    The public entry point: asks the model for company_name's careers page, then independently
    re-verifies whatever it claims before returning anything.

    Args:
        company_name: The company to research.
        domain: The company's known domain, when available.
        client: Forwarded to llm_resolve(); tests inject a fake instead of hitting the network.
        model: Forwarded to llm_resolve().

    Returns:
        (result, reason). result is a verified ResolveResult(resolved_by="llm"), or None when the
        model call failed/was skipped or its answer could not be independently confirmed. reason is
        "" whenever the model call itself succeeded (even if verification then found nothing to
        confirm — an unconfirmed claim is not a call failure), or llm_resolve()'s own failure reason
        when the call itself failed or was skipped.

    Raises:
        Never raises.
    """
    result, reason = llm_resolve(company_name, domain, client=client, model=model)
    if result is None:
        return None, reason
    return verify_llm_result(result, company_name, domain), ""
