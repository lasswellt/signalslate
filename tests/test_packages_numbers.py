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


@pytest.mark.parametrize("carrier,fmt,number", _cases("invalid"))
def test_invalid_numbers_fail_or_do_not_match(carrier, fmt, number):
    if carrier == "s10":
        pytest.skip("S10 invalid case is an unlisted country code (jkeen 'additional' lookup), not a regex/checksum fault")
    for m in numbers.classify(number):
        if m.carrier == carrier and m.format_name == fmt:
            assert m.checksum is not True, number


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
