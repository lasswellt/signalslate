"""
Tracking-number pattern matcher and checksum validators (jkeen/tracking_number_data rules).

Design decisions:

- Rules are the vendored jkeen courier JSON under pipeline/packages/data/couriers/, loaded once and
  compiled in compact form (the ``\\s*`` spread jkeen uses for printed numbers is removed) because
  callers scan tokens, not whitespace-spread text. Named groups are converted from the .NET/Ruby
  ``(?<n>`` syntax to Python's ``(?P<n>`` and duplicate names are suffixed so alternations compile.
- Checksums follow jkeen's definitions exactly: letters map to digits via (ord(c)-3) % 10 (UPS
  ``1Z`` bodies, S10 and FedEx alpha serials). A format whose checksum name is unknown (e.g. DPD
  mod_37_36) or whose check digit is not numeric reports ``checksum=None``, i.e. "unverified",
  never a false pass.
- A checksum a random number passes too often (DHL Express mod7 passes ~1 in 7, Purolator (12)
  Luhn ~1 in 10) is flagged ``weak``; best_match only accepts it with carrier/waybill context.
  Formats with no checksum are accepted only when the caller saw a tracking label next to them.
- Pure functions, no I/O beyond the one-time rule load, no logging of scanned text (privacy: mail
  bodies are never persisted or logged).
"""
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

_DATA_DIR = Path(__file__).parent / "data" / "couriers"
_WEAK_FORMATS = frozenset({("dhl", "DHL Express"), ("purolator", "Purolator (12)")})
_NAMED_GROUP = re.compile(r"\(\?<(\w+)>")


@dataclass(frozen=True)
class Match:
    """
    A tracking-number format a token matched.

    Attributes:
        carrier: jkeen courier_code (e.g. "ups", "usps", "fedex", "dhl").
        format_name: jkeen format name (e.g. "FedEx Express (12)").
        checksum: True passed, False failed, None format has no (supported) checksum.
        weak: True when the validator passes too often by chance (DHL Express mod7, Purolator (12)).
    """

    carrier: str
    format_name: str
    checksum: Optional[bool]
    weak: bool


@dataclass(frozen=True)
class _Rule:
    carrier: str
    name: str
    search: "re.Pattern[str]"
    full: "re.Pattern[str]"
    validation: dict[str, Any]


def _convert(regex: str) -> str:
    """Converts a jkeen regex to a compact Python one (named groups, unique names, no ``\\s*``)."""
    seen: dict[str, int] = {}

    def rename(m: "re.Match[str]") -> str:
        name = m.group(1)
        seen[name] = seen.get(name, 0) + 1
        return "(?P<%s>" % (name if seen[name] == 1 else "%s__%d" % (name, seen[name]))

    return _NAMED_GROUP.sub(rename, regex).replace(r"\s*", "")


def _digits(s: str) -> list[int]:
    return [int(c) if c.isdigit() else (ord(c) - 3) % 10 for c in s]


def _mod10(ser: str, chk: int, v: dict[str, Any]) -> bool:
    s = ser[::-1] if v.get("reverse") else ser
    even, odd = v.get("evens_multiplier", 1), v.get("odds_multiplier", 1)
    total = sum(x * (odd if i % 2 else even) for i, x in enumerate(_digits(s)))
    rem = total % 10
    return (0 if rem == 0 else 10 - rem) == chk


def _sum_product_with_weightings_and_modulo(ser: str, chk: int, v: dict[str, Any]) -> bool:
    total = sum(a * b for a, b in zip(_digits(ser), v["weightings"]))
    return total % v["modulo1"] % v["modulo2"] == chk


def _mod7(ser: str, chk: int, v: dict[str, Any]) -> bool:
    return int(ser) % 7 == chk


def _s10(ser: str, chk: int, v: dict[str, Any]) -> bool:
    total = sum(a * b for a, b in zip(map(int, ser), [8, 6, 4, 2, 3, 5, 9, 7]))
    c = 11 - total % 11
    return {10: 0, 11: 5}.get(c, c) == chk


def _luhn(ser: str, chk: int, v: dict[str, Any]) -> bool:
    total = 0
    for i, x in enumerate(reversed(_digits(ser))):
        x = x * 2 if i % 2 == 0 else x
        total += x - 9 if x > 9 else x
    return (10 - total % 10) % 10 == chk


_CHECKSUMS: dict[str, Callable[[str, int, dict[str, Any]], bool]] = {
    "mod10": _mod10,
    "sum_product_with_weightings_and_modulo": _sum_product_with_weightings_and_modulo,
    "mod7": _mod7,
    "s10": _s10,
    "luhn": _luhn,
}


@lru_cache(maxsize=1)
def load_rules() -> tuple[_Rule, ...]:
    """
    Loads and compiles every rule in the vendored courier JSON (cached).

    Returns:
        Compiled rules in courier-file then declaration order.

    Raises:
        OSError: a courier file cannot be read.
        ValueError: a courier file is not valid JSON.
        re.error: a vendored regex does not compile after conversion.
    """
    rules: list[_Rule] = []
    for path in sorted(_DATA_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        carrier = data.get("courier_code") or data["name"]
        for t in data["tracking_numbers"]:
            raw = t["regex"] if isinstance(t["regex"], str) else "".join(t["regex"])
            body = _convert(raw)
            rules.append(_Rule(
                carrier=carrier,
                name=t["name"],
                search=re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![a-z0-9])"),
                full=re.compile("^" + body + "$"),
                validation=t.get("validation", {}),
            ))
    return tuple(rules)


def _prepend(ser: str, validation: dict[str, Any]) -> str:
    """Applies a serial_number_format prepend_if rule (USPS Legacy, OnTrac)."""
    rule = (validation.get("serial_number_format") or {}).get("prepend_if")
    if rule and re.match(rule["matches_regex"], ser):
        return rule["content"] + ser
    return ser


def _check(rule: _Rule, m: "re.Match[str]") -> Optional[bool]:
    ck = rule.validation.get("checksum")
    if not ck:
        return None
    fn = _CHECKSUMS.get(ck["name"])
    groups = m.groupdict()
    digit = groups.get("CheckDigit")
    if fn is None or not digit or not digit.isdigit():
        return None
    ser = _prepend(groups.get("SerialNumber") or "", rule.validation)
    try:
        return fn(ser, int(digit), ck)
    except (ValueError, KeyError):
        return None


def classify(token: str) -> list[Match]:
    """
    Full-matches a token against every format and runs each format's checksum.

    Args:
        token: Candidate tracking number; upper-cased, whitespace stripped, hyphens stripped
            unless the format itself contains one.

    Returns:
        One Match per format the token fits (possibly empty).
    """
    spaced = re.sub(r"\s", "", token).upper()
    forms = (spaced, spaced.replace("-", ""))  # hyphen is literal in LaserShip 1LS7 (18)
    out: list[Match] = []
    for rule in load_rules():
        m = next((x for x in (rule.full.match(f) for f in forms) if x), None)
        if m:
            out.append(Match(rule.carrier, rule.name, _check(rule, m),
                             (rule.carrier, rule.name) in _WEAK_FORMATS))
    return out


def find_candidates(text: str) -> list[tuple[str, int, int]]:
    """
    Scans text for every token any format's pattern matches.

    A match must not touch a preceding alphanumeric or a following lowercase letter/digit, so a
    capitalised word glued on by HTML-to-text conversion ("1541729044Track") is allowed.

    Args:
        text: Visible text (URLs already stripped by the caller).

    Returns:
        Deduplicated (token, start, end) tuples in discovery order.
    """
    seen: set[tuple[str, int, int]] = set()
    out: list[tuple[str, int, int]] = []
    for rule in load_rules():
        for m in rule.search.finditer(text):
            item = (m.group(0), m.start(), m.end())
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def best_match(token: str, *, context: str, labeled: bool) -> Optional[Match]:
    """
    Picks the strongest accepted format for a token.

    Strong checksum pass is accepted; a weak pass only when the carrier name or "waybill" occurs in
    context; a checksum-less format only when labeled; a checksum failure is rejected. Preference:
    strong pass, then weak pass, then checksum-less.

    Args:
        token: Candidate tracking number.
        context: Text near the token (and sender) used for the weak-validator rule.
        labeled: True when the token follows a tracking label or came from a tracking URL.

    Returns:
        The accepted Match, or None.
    """
    ctx = context.lower()
    tiers: list[list[Match]] = [[], [], []]
    for m in classify(token):
        if m.checksum is True and not m.weak:
            tiers[0].append(m)
        elif m.checksum is True:
            names = {m.carrier.lower(), m.carrier.lower().replace("_", " ")}
            if "waybill" in ctx or any(n in ctx for n in names):
                tiers[1].append(m)
        elif m.checksum is None and labeled:
            tiers[2].append(m)
    for tier in tiers:
        if tier:
            return tier[0]
    return None
