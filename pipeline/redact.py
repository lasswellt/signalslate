"""
One redaction function for every text that is persisted (SourceHealth.detail, Run.error,
Run.summary) or returned by the API (docs/_research/2026-09-21_management-ui.md section 2).
Those strings carry str(exc) and slices of upstream error bodies, so a credential can ride along
in an exception message or an echoed request.

Design decisions:

- Never raises. It runs inside error paths; an exception here would mask the original failure and
  could itself carry the text it was asked to scrub. On an unexpected internal failure it fails
  closed and returns the marker alone.
- Every source (supplied secret values, token shapes, key=value pairs) contributes character SPANS
  that are merged and replaced once. Sequential str.replace calls cannot do that: overlapping
  secrets would leave a fragment of the second one behind, and a secret that happens to be a
  substring of the marker would corrupt it.
- Idempotence comes from splitting on the marker first and redacting each piece on its own, so an
  already-redacted value is never re-matched (a bare `]` inside a value class would otherwise turn
  `[redacted]` into `[redacted]]`).
- Linear time. No nested quantifiers; the quoted-value alternative uses possessive quantifiers so
  an unterminated quote costs one scan instead of a backtracking search; the JWT pattern carries a
  lookbehind so a run of `eyJeyJeyJ...` cannot start a new O(n) attempt at every position.
  tests/test_redact.py feeds 200000-character adversarial inputs to hold that line.
- Over-redaction is the accepted failure mode; a mangled diagnostic is cheaper than a leaked token.
  The one carve-out is a bare 1 to 3 digit `code` value, which is an HTTP status in upstream error
  bodies ({"error": {"code": 403}}) and never an OAuth authorization code.
"""
import json
import re
from typing import Iterable, Optional
from urllib.parse import quote, quote_plus

MARKER = "[redacted]"

# Below this length a value is more likely an ordinary word than a credential, and replacing it
# would shred unrelated text.
MIN_SECRET_LEN = 6

_TOKEN_SHAPES = [
    re.compile(r"xox[abprs]-[A-Za-z0-9-]+"),
    re.compile(r"ya29\.[A-Za-z0-9_-]+"),
    re.compile(r"(?<![A-Za-z0-9])1//[A-Za-z0-9_-]{8,}"),
    re.compile(r"GOCSPX-[A-Za-z0-9_-]+"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    # Lookbehind, see the module docstring: keeps the scan linear on repeated `eyJ`.
    re.compile(r"(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),
]

# A bare "Bearer <token>" outside an Authorization header. The 16 character floor keeps prose such
# as "Bearer authentication" intact.
_BARE_BEARER = re.compile(r"(?<!\w)Bearer[ \t]+(?P<val>[A-Za-z0-9._~+/=-]{16,})")

# The scheme word is skipped, not captured, so the output keeps `Authorization: Bearer [redacted]`.
# The lookahead stops a lone scheme word being taken for the value when the token was already cut.
_AUTHORIZATION = re.compile(
    r"""authorization\b["']?[ \t]*[:=][ \t]*["']?(?:(?:bearer|basic)[ \t]+)?"""
    r"""(?P<val>(?!(?:bearer|basic)\b)[^\s"',;]+)""",
    re.IGNORECASE,
)

# The unterminated-quote alternative exists because upstream bodies are stored sliced
# (resp.text[:200]), so a value is often cut before its closing quote.
_VALUE = (
    r"""(?:"(?P<dq>(?:[^"\\]|\\.)*+)"|'(?P<sq>(?:[^'\\]|\\.)*+)'"""
    r"""|["'](?P<open>[^\s"']+)|(?P<bare>[^\s"'&,;}\]]+))"""
)

# `code` alone is only matched as `code=` or a quoted JSON key, and never as a suffix (error_code,
# status_code): "status code: 500" is ordinary text. The longer names also match as a suffix so
# GOOGLE_CLIENT_SECRET=... is caught.
_PAIR = re.compile(
    r"""(?:(?:refresh_token|access_token|client_secret|code_verifier|password)["']?[ \t]*[:=]"""
    r"""|(?<![A-Za-z0-9_])(?P<codekey>code)(?:["'][ \t]*[:=]|[ \t]*=))[ \t]*""" + _VALUE,
    re.IGNORECASE,
)

_HTTP_STATUS_LIKE = re.compile(r"\d{1,3}")


def _secret_forms(secrets: Iterable[str]) -> list[str]:
    """
    Every text form a supplied secret can take once it has been echoed into a URL or a JSON body.
    A single str is treated as one secret; iterating it would yield one-character "secrets" that
    are all ignored, which would silently disable the feature.
    """
    if isinstance(secrets, str):
        secrets = [secrets]
    forms: set[str] = set()
    for secret in secrets:
        if not isinstance(secret, str) or len(secret) < MIN_SECRET_LEN or not secret.strip():
            continue
        forms.update(
            (
                secret,
                quote(secret, safe=""),
                quote_plus(secret),
                json.dumps(secret)[1:-1],
                json.dumps(secret, ensure_ascii=False)[1:-1],
            )
        )
    return [f for f in forms if len(f) >= MIN_SECRET_LEN]


def _spans(text: str, forms: list[str]) -> list[tuple[int, int]]:
    """Collects the half-open spans of everything in text that must be replaced."""
    spans: list[tuple[int, int]] = []
    for form in forms:
        i = text.find(form)
        while i != -1:
            spans.append((i, i + len(form)))
            i = text.find(form, i + 1)  # +1, not +len: overlapping hits must all be covered
    for rx in _TOKEN_SHAPES:
        spans.extend(m.span() for m in rx.finditer(text))
    for m in _BARE_BEARER.finditer(text):
        spans.append(m.span("val"))
    for m in _AUTHORIZATION.finditer(text):
        spans.append(m.span("val"))
    for m in _PAIR.finditer(text):
        group = next(g for g in ("dq", "sq", "open", "bare") if m.group(g) is not None)
        if m.group("codekey") and _HTTP_STATUS_LIKE.fullmatch(m.group(group)):
            continue
        spans.append(m.span(group))
    return [s for s in spans if s[1] > s[0]]


def _redact_piece(text: str, forms: list[str]) -> str:
    spans = _spans(text, forms)
    if not spans:
        return text
    spans.sort()
    # Touching spans merge too, so two adjacent secrets leave one marker rather than two.
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out: list[str] = []
    pos = 0
    for start, end in merged:
        out.append(text[pos:start])
        out.append(MARKER)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def redact(text: object, secrets: Iterable[str] = (), *, max_len: Optional[int] = None) -> str:
    """
    Replaces credentials in text with [redacted], then truncates to max_len when given.

    Truncation comes after redaction on purpose: cutting first could slice a token in half, and
    the remaining fragment would no longer match its shape.

    Args:
        text: Anything; a non-str is converted with str().
        secrets: Known secret values to scrub, in plain, URL-encoded and JSON-escaped form.
            Values shorter than 6 characters are ignored.
        max_len: Maximum length of the result; None for no limit.

    Returns:
        The scrubbed text. Never raises: on an unexpected internal error it returns the marker.
    """
    try:
        try:
            plain = text if isinstance(text, str) else str(text)
        except Exception:
            plain = MARKER
        forms = _secret_forms(secrets)
        result = MARKER.join(_redact_piece(piece, forms) for piece in plain.split(MARKER))
    except Exception:
        result = MARKER
    if isinstance(max_len, int) and not isinstance(max_len, bool):
        result = result[: max(0, max_len)]
    return result
