"""Tests for pipeline.packages.numbers against the vendored jkeen courier test numbers."""
import json
from pathlib import Path

import pytest

from pipeline.packages import numbers

_DIR = Path(numbers.__file__).parent / "data" / "couriers"


def _cases(kind: str) -> list[tuple[str, str, str]]:
    out = []
    for p in sorted(_DIR.glob("*.json")):
        d = json.loads(p.read_text())
        for t in d["tracking_numbers"]:
            for n in t["test_numbers"][kind]:
                out.append((d["courier_code"], t["name"], n))
    return out


@pytest.mark.parametrize("carrier,fmt,number", _cases("valid"))
def test_valid_numbers_classify(carrier, fmt, number):
    ms = [m for m in numbers.classify(number) if m.carrier == carrier and m.format_name == fmt]
    assert ms, number
    has_ck = "checksum" in next(
        t for p in _DIR.glob("*.json") for t in json.loads(p.read_text())["tracking_numbers"]
        if t["name"] == fmt)["validation"]
    assert ms[0].checksum in ((True, None) if has_ck else (None,))


def _declares_checksum(fmt: str) -> bool:
    return "checksum" in next(
        t for p in _DIR.glob("*.json") for t in json.loads(p.read_text())["tracking_numbers"]
        if t["name"] == fmt)["validation"]


# DPD's mod_37_36 checksum is not implemented (documented design decision: unknown checksum reports
# None = unverified, never a false pass), so these jkeen "invalid" samples cannot be told apart from
# valid numbers by this matcher. Strict xfail: if the checksum is ever implemented they must flip.
_UNDETECTABLE = {
    ("dpd", "DPD (28)", "008182709980000020033350276A"),
    ("dpd", "DPD (14)", "09980000020033D"),
}


def _invalid_params():
    return [pytest.param(*c, marks=pytest.mark.xfail(
        strict=True, reason="DPD mod_37_36 checksum unsupported; matcher reports unverified (None)"))
        if c in _UNDETECTABLE else pytest.param(*c) for c in _cases("invalid")]


@pytest.mark.parametrize("carrier,fmt,number", _invalid_params())
def test_invalid_numbers_fail_or_do_not_match(carrier, fmt, number):
    """An invalid sample must not be accepted as valid by the same carrier/format.

    Accepted means checksum True, or checksum None for a format that has no checksum at all (those
    pass on shape alone). A failed checksum, a checksum-declaring format that cannot verify, or no
    match at all counts as not accepted.
    """
    has_ck = _declares_checksum(fmt)
    for m in numbers.classify(number):
        if m.carrier == carrier and m.format_name == fmt:
            # no-checksum format: a shape match is acceptance; checksum format: must fail outright
            assert has_ck and m.checksum is False, (number, m)


def test_s10_unlisted_country_code_rejected():
    assert not [m for m in numbers.classify("RB123456785XX") if m.carrier == "s10"]
    assert [m for m in numbers.classify("RB123456785GB") if m.carrier == "s10"]


def test_dhl_express_weak_requires_context():
    assert numbers.best_match("3318810025", context="call us at", labeled=False) is None
    m = numbers.best_match("3318810025", context="DHL waybill", labeled=False)
    assert m and m.carrier == "dhl" and m.weak
    assert numbers.best_match("3318810025", context="your waybill", labeled=False)


def test_checksumless_needs_label():
    tok = "12345678901234"
    assert numbers.best_match(tok, context="", labeled=False) is None


def test_checksum_failure_rejected():
    assert numbers.best_match("1Z999AA10123456785", context="UPS", labeled=True) is None


def test_ups_strong_accepted_without_context():
    m = numbers.best_match("1Z999AA10123456784", context="", labeled=False)
    assert m and m.carrier == "ups" and m.checksum is True and not m.weak


def test_glued_capitalised_word_found():
    toks = [t for t, _, _ in numbers.find_candidates("Tracking 1541729044Track your parcel")]
    assert "1541729044" in toks


def test_find_candidates_respects_boundaries():
    toks = [t for t, _, _ in numbers.find_candidates("x1541729044 and 15417290449z")]
    assert "1541729044" not in toks
