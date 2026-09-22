"""
Domain name ideas: local combinatorial generation, optional LLM brainstorming and a private
availability prefilter (Research Q5, Finding 6).

Design decisions:

- generate_candidates() is pure and deterministic: seeds x affixes x tlds walked in one fixed
  nested order, each candidate normalized through pipeline.domains.normalize_domain() (the one
  place a raw label becomes a domain name every other module trusts) and the whole run capped at
  _MAX_CANDIDATES so a caller can never make a UI page, or a downstream registrar/RDAP check, pay
  for an unbounded fan-out.
- llm_ideas() reuses pipeline.triage.make_client()/pipeline.health.llm_settings() instead of
  wiring a second Anthropic client (research doc: "no new LLM dependency; reuses the existing
  Anthropic client"). Model output is untrusted input exactly like triage's map stage: every name
  is normalize_domain()-checked and anything that fails is dropped, never surfaced raw. An
  unconfigured key, a refusal or any SDK/parse failure all degrade to ([], reason) rather than
  raising, mirroring triage_items()'s "a failed call degrades, it never kills the run" convention;
  reason never carries the exception message (pipeline.redact / pipeline.runner convention: log
  the exception CLASS name only, since a message can carry request/response detail).
- prefilter() never queries a registrar or a third-party "domain search" site: front-running an
  unregistered name off a public search query is a documented harm (research doc, the 2008 Network
  Solutions case). It only reuses pipeline.domains.dns.lookup_records() and
  pipeline.domains.rdap.lookup_rdap(), both already private/local checks, and stays honest about
  the limit that implies: lookup_records()'s own docstring says NXDOMAIN and NoAnswer come back
  identically (ok=True, values=()), so "no record of any type resolved" is the closest signal this
  layer can give to "NXDOMAIN". Combined with RDAP status="not_found" that is a decent private
  hint, never proof — premium, reserved and redemption-period names still false-positive as
  "likely_available" here (research doc), which is exactly why the caller sends only a short
  authoritative-check shortlist to a registrar, never this prefilter's output directly.
"""
import json
import logging
from collections.abc import Callable, Sequence
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

from pipeline.domains import normalize_domain
from pipeline.domains.dns import RecordResult, lookup_records
from pipeline.domains.rdap import lookup_rdap
from pipeline.health import llm_settings
from pipeline.triage import TriageConfigError, make_client

_log = logging.getLogger(__name__)

_MAX_CANDIDATES = 500
_DEFAULT_MAX_LEN = 63

_IDEAS_MAX_TOKENS = 1024
_IDEAS_SYSTEM_PROMPT = """\
You generate short, brandable domain name ideas for a product or project brief.
Return each idea as one full domain name: a label plus a top-level domain, e.g. "exampleapp.com" \
or "example.io". Lower-case only, no spaces, no explanation, no markdown, no URLs, and avoid \
existing well-known trademarks."""


class _IdeaRecord(BaseModel):
    """One model-proposed domain, before normalize_domain() validates it."""

    model_config = ConfigDict(extra="forbid")

    name: str


class _IdeaBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ideas: list[_IdeaRecord]


def _raw_labels(seeds: Sequence[str], affixes: Sequence[str], allow_hyphen: bool) -> list[str]:
    """seed-major, affix-minor label variants: seed alone, affix+seed and seed+affix, plus their
    hyphenated forms when allow_hyphen. Blank seeds/affixes are skipped rather than producing a
    malformed label."""
    labels: list[str] = []
    for raw_seed in seeds:
        seed = raw_seed.strip().lower()
        if not seed:
            continue
        labels.append(seed)
        for raw_affix in affixes:
            affix = raw_affix.strip().lower()
            if not affix:
                continue
            labels.append(f"{affix}{seed}")
            labels.append(f"{seed}{affix}")
            if allow_hyphen:
                labels.append(f"{affix}-{seed}")
                labels.append(f"{seed}-{affix}")
    return labels


def generate_candidates(
    seeds: Sequence[str],
    tlds: Sequence[str],
    affixes: Sequence[str] = (),
    max_len: int = _DEFAULT_MAX_LEN,
    allow_hyphen: bool = False,
) -> list[str]:
    """
    Deterministically builds candidate domains from seeds x affixes x tlds.

    Walks seeds in order; for each seed, the bare seed then every affix combination (prefix,
    suffix, and their hyphenated forms when allow_hyphen); for each label, every tld in order.
    Every candidate is normalized via pipeline.domains.normalize_domain(); a label over max_len or
    one that fails normalization (invalid IDNA, a stray scheme/path, etc.) is dropped rather than
    raised. Duplicates (across affix/tld combinations) are dropped, keeping first-seen order.

    Args:
        seeds: Seed words/brand fragments, any case/whitespace.
        tlds: TLDs to combine with, with or without a leading dot.
        affixes: Optional prefix/suffix fragments to combine with each seed.
        max_len: Max label (not full-domain) length before normalize_domain() even runs.
        allow_hyphen: Also emit affix-seed and seed-affix joined by a hyphen.

    Returns:
        Normalized, deduplicated candidate domains, capped at _MAX_CANDIDATES (500), in generation
        order. Never raises: an empty seeds/tlds list, or one that yields nothing valid, is an
        empty list, not an error.
    """
    out: list[str] = []
    seen: set[str] = set()
    for label in _raw_labels(seeds, affixes, allow_hyphen):
        if len(label) > max_len:
            continue
        for raw_tld in tlds:
            tld = raw_tld.strip().lower().lstrip(".")
            if not tld:
                continue
            try:
                name, _tld = normalize_domain(f"{label}.{tld}")
            except ValueError:
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
            if len(out) >= _MAX_CANDIDATES:
                return out
    return out


def llm_ideas(
    brief: str,
    n: int = 20,
    *,
    client: Optional[Any] = None,
    model: Optional[str] = None,
) -> tuple[list[str], str]:
    """
    Asks the Anthropic model for up to n candidate domain names from a short brief.

    Reuses pipeline.triage.make_client()/pipeline.health.llm_settings() rather than a second LLM
    wiring path. Model output is untrusted: every returned name passes through
    pipeline.domains.normalize_domain(); anything that fails, or repeats a name already kept, is
    dropped rather than surfaced.

    Args:
        brief: A short description of what the domain is for.
        n: How many ideas to ask for; also the cap on names returned.
        client: An anthropic.Anthropic-like object (duck-typed: messages.parse(**kwargs) returns
            an object with parsed_output/stop_reason). Tests inject a fake here instead of hitting
            the network. Defaults to pipeline.triage.make_client().
        model: Overrides llm_settings()["map_model"]; tests pass a fixed model id.

    Returns:
        (names, reason). names is a deduplicated, normalize_domain()-valid candidate list, capped
        at n, in the model's own order. reason is "" on success (even when the model returned zero
        usable names after filtering), or a short, non-sensitive explanation — an unconfigured key,
        a refusal, or the failing exception's CLASS name (never its message) — whenever the call
        was skipped or failed. Never raises for a configuration or model/API failure; a partial
        (combinatorial-only) idea list beats a dead ideas panel.

    Raises:
        ValueError: brief is blank or n < 1 (a caller bug, not a runtime/LLM failure).
    """
    if not brief or not brief.strip():
        raise ValueError("brief must not be empty")
    if n < 1:
        raise ValueError("n must be >= 1")

    active_client = client
    if active_client is None:
        try:
            active_client = make_client()
        except TriageConfigError as exc:
            return [], str(exc)

    active_model = model if model is not None else llm_settings()["map_model"]
    request = {
        "model": active_model,
        "max_tokens": _IDEAS_MAX_TOKENS,
        "system": _IDEAS_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps({"brief": brief, "count": n}, ensure_ascii=False)}],
        "output_format": _IdeaBatch,
    }

    try:
        response = active_client.messages.parse(**request)
    except (anthropic.APIError, ValidationError) as exc:
        return [], type(exc).__name__
    except Exception as exc:
        # Class name only: an arbitrary exception here can echo the request or response text.
        return [], type(exc).__name__

    if response.stop_reason in ("refusal", "max_tokens"):
        return [], f"stop_reason={response.stop_reason}"
    if response.parsed_output is None:
        return [], "no parsed output"

    names: list[str] = []
    seen: set[str] = set()
    for idea in response.parsed_output.ideas:
        try:
            name, _tld = normalize_domain(idea.name)
        except ValueError:
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= n:
            break
    return names, ""


def _dns_signal(records: dict[str, RecordResult]) -> str:
    """'present' when any record type resolved to a value; 'absent' when every type queried
    cleanly and found nothing (lookup_records() cannot tell NXDOMAIN from NoAnswer apart, per its
    own docstring); 'error' when at least one lookup itself failed and none had values."""
    if any(result.ok and result.values for result in records.values()):
        return "present"
    if all(result.ok for result in records.values()):
        return "absent"
    return "error"


def prefilter(
    names: Sequence[str],
    *,
    resolver: Optional[Any] = None,
    domain_lookup: Optional[Callable[[str], dict]] = None,
    bootstrap: Optional[Callable[..., None]] = None,
    is_bootstrapped: Optional[Callable[[], bool]] = None,
) -> dict[str, str]:
    """
    A private, local-only availability signal for each name: no registrar or third-party search
    call is ever made (front-running risk; see module docstring).

    Args:
        names: Candidate domains, any form normalize_domain() accepts.
        resolver: Forwarded to pipeline.domains.dns.lookup_records(); tests inject a fake resolver
            instead of hitting the network. Defaults to a real resolver there.
        domain_lookup: Forwarded to pipeline.domains.rdap.lookup_rdap() when set; tests inject a
            fake instead of hitting the network. Unset keeps lookup_rdap()'s own default
            (whoisit.domain).
        bootstrap: Same as domain_lookup, for lookup_rdap()'s bootstrap parameter.
        is_bootstrapped: Same as domain_lookup, for lookup_rdap()'s is_bootstrapped parameter.

    Returns:
        dict keyed by the normalized name (or the raw input, for one that fails
        normalize_domain()), each value one of "likely_available" (no DNS record of any type, and
        RDAP reports not_found), "taken" (any DNS record resolved, or RDAP reports the domain is
        registered), or "unknown" (a failed/unsupported lookup, an invalid name, or any other
        combination that is not conclusive either way). Never raises.
    """
    rdap_kwargs: dict[str, Any] = {}
    if domain_lookup is not None:
        rdap_kwargs["domain_lookup"] = domain_lookup
    if bootstrap is not None:
        rdap_kwargs["bootstrap"] = bootstrap
    if is_bootstrapped is not None:
        rdap_kwargs["is_bootstrapped"] = is_bootstrapped

    out: dict[str, str] = {}
    for raw in names:
        try:
            name, _tld = normalize_domain(raw)
        except ValueError:
            out[raw] = "unknown"
            continue

        dns_signal = _dns_signal(lookup_records(name, resolver=resolver))
        if dns_signal == "present":
            out[name] = "taken"
            continue

        rdap_result = lookup_rdap(name, **rdap_kwargs)
        if rdap_result.status == "ok":
            out[name] = "taken"
        elif dns_signal == "absent" and rdap_result.status == "not_found":
            out[name] = "likely_available"
        else:
            out[name] = "unknown"
    return out
