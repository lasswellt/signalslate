"""
Unit tests for pipeline.redact. Pure function, nothing mocked. Every credential below is an
invented value; none is a real token shape that would authenticate anywhere.
"""
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.redact import MARKER, redact  # noqa: E402

SLACK = "xoxb-1234567890-abcdefghijkl"
YA29 = "ya29.a0Invented_Access-Token123"
REFRESH = "1//0gInventedRefreshToken_abc-123"
GOCSPX = "GOCSPX-InventedClientSecret_12-3"
ANTHROPIC = "sk-ant-api03-InventedKey_abc-123"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"


@pytest.mark.parametrize(
    "secret",
    [SLACK, YA29, REFRESH, GOCSPX, ANTHROPIC, JWT],
    ids=["slack", "ya29", "refresh-1//", "gocspx", "anthropic", "jwt"],
)
def test_token_shape_is_redacted(secret):
    out = redact(f"upstream said: token {secret} was rejected")
    assert secret not in out
    assert out == f"upstream said: token {MARKER} was rejected"


@pytest.mark.parametrize("prefix", ["xoxa", "xoxp", "xoxr", "xoxs"])
def test_every_slack_token_kind(prefix):
    assert redact(f"{prefix}-11-22-abcdef") == MARKER


@pytest.mark.parametrize(
    "text, secret",
    [
        ("Authorization: Bearer abc123.def-456", "abc123.def-456"),
        ("authorization: bearer abc123def", "abc123def"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ('"Authorization": "Bearer abc123def"', "abc123def"),
        ("Authorization=Token123abc", "Token123abc"),
        ("sent Bearer abcdefghijklmnop1234 to server", "abcdefghijklmnop1234"),
    ],
)
def test_authorization_values(text, secret):
    out = redact(text)
    assert secret not in out
    assert MARKER in out


def test_authorization_keeps_scheme_word():
    assert redact("Authorization: Bearer abc123def") == f"Authorization: Bearer {MARKER}"


def test_bare_bearer_prose_is_not_mangled():
    assert redact("Bearer authentication failed") == "Bearer authentication failed"


@pytest.mark.parametrize(
    "text, secret",
    [
        ("refresh_token=abc123xyz", "abc123xyz"),
        ("access_token=abc123xyz&x=1", "abc123xyz"),
        ("client_secret=s3cr3t-value", "s3cr3t-value"),
        ("code_verifier=verifierValue123", "verifierValue123"),
        ("code=4/0AbCdEf", "4/0AbCdEf"),
        ("password=hunter22", "hunter22"),
        ("PASSWORD: hunter22", "hunter22"),
        ("GOOGLE_CLIENT_SECRET=plainsecret", "plainsecret"),
        ('{"refresh_token": "abc def 123"}', "abc def 123"),
        ('{"password":"p@ss word","ok":true}', "p@ss word"),
        ("{'client_secret': 'single-quoted'}", "single-quoted"),
        ('{"code": "4/0AbCdEf"}', "4/0AbCdEf"),
        ("GET /cb?state=s1&code=4%2F0AbCdEf&scope=x", "4%2F0AbCdEf"),
    ],
)
def test_key_value_and_json_pairs(text, secret):
    out = redact(text)
    assert secret not in out
    assert MARKER in out


def test_pair_keeps_key_structure():
    assert redact('{"password": "hunter22", "user": "bob"}') == (
        f'{{"password": "{MARKER}", "user": "bob"}}'
    )
    assert redact("a=1&code=xyz789&b=2") == f"a=1&code={MARKER}&b=2"


def test_truncated_body_with_unterminated_quote_is_still_redacted():
    out = redact('{"client_secret": "abcdef123456')
    assert "abcdef123456" not in out


def test_http_status_code_is_not_mistaken_for_an_oauth_code():
    body = '{"error": {"code": 403, "message": "denied"}}'
    assert redact(body) == body
    assert redact("status code: 500 error_code=42 status_code=200") == (
        "status code: 500 error_code=42 status_code=200"
    )


SECRET = "s3cr3t/value+with space&more"


def test_supplied_secret_plain():
    assert redact(f"failed with {SECRET} in body", [SECRET]) == f"failed with {MARKER} in body"


def test_supplied_secret_url_encoded_forms():
    for encoded in (quote(SECRET, safe=""), quote_plus(SECRET)):
        out = redact(f"GET /x?v={encoded}", [SECRET])
        assert encoded not in out
        assert out == f"GET /x?v={MARKER}"


def test_supplied_secret_json_escaped_forms():
    secret = 'pa"ss\\word\nline é'
    for escaped in (json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
        out = redact('{"echo": "' + escaped + '"}', [secret])
        assert escaped not in out
        assert out == '{"echo": "' + MARKER + '"}'


def test_short_supplied_secrets_are_ignored():
    assert redact("the word admin and abcde appear", ["admin", "abcde", ""]) == (
        "the word admin and abcde appear"
    )
    assert redact("exactly abcdef here", ["abcdef"]) == f"exactly {MARKER} here"


def test_whitespace_only_secret_is_ignored():
    assert redact("a      b", ["      "]) == "a      b"


def test_single_string_is_one_secret_not_a_character_iterable():
    assert redact("x hunter22 y", "hunter22") == f"x {MARKER} y"


def test_non_str_secrets_are_skipped():
    mixed: Any = [None, 12345678, b"hunter22", "hunter22"]
    assert redact("x hunter22 y", mixed) == f"x {MARKER} y"


def test_overlapping_secrets_leave_no_fragment():
    out = redact("abcdefghi", ["abcdef", "defghi"])
    assert out == MARKER
    out = redact("k=abcdefghij", ["abcdef", "abcdefghij"])
    assert out == f"k={MARKER}"


def test_adjacent_secrets_merge_into_one_marker():
    assert redact("abcdefghijkl", ["abcdef", "ghijkl"]) == MARKER


def test_secret_that_is_a_substring_of_the_marker_does_not_corrupt_it():
    once = redact("the redacted word and xoxb-1-2-abc", ["redact"])
    assert once == f"the {MARKER}ed word and {MARKER}"
    assert redact(once, ["redact"]) == once


def test_secret_and_pattern_overlap_yields_one_marker():
    assert redact(f"password={SLACK}", [SLACK]) == f"password={MARKER}"


@pytest.mark.parametrize(
    "value, expected",
    [
        (12345, "12345"),
        (None, "None"),
        (3.5, "3.5"),
        (["a", "b"], "['a', 'b']"),
        (b"bytes", "b'bytes'"),
        (ValueError("boom"), "boom"),
    ],
)
def test_non_str_input_is_converted(value, expected):
    assert redact(value) == expected


def test_non_str_input_is_still_redacted():
    assert redact(ValueError(f"bad token {SLACK}")) == f"bad token {MARKER}"


def test_never_raises_when_str_fails():
    class Hostile:
        def __str__(self):
            raise RuntimeError("no")

    assert redact(Hostile()) == MARKER


def test_never_raises_on_unusable_secrets_argument():
    not_iterable: Any = 5
    assert redact("plain text", not_iterable) == MARKER


def test_max_len_truncates_after_redaction():
    text = f"{SLACK} " + "x" * 100
    out = redact(text, max_len=30)
    assert len(out) == 30
    assert out.startswith(MARKER)
    assert SLACK not in out


def test_max_len_does_not_leave_half_a_token_behind():
    # Cutting first would leave "yyyyyxoxb-123", which still matches; the real hazard is a cut that
    # leaves a fragment too short to match, e.g. 8 chars of a 27 char token.
    out = redact("y" * 5 + SLACK, max_len=12)
    assert out == "yyyyy[redact"
    assert "xoxb" not in out


def test_max_len_edge_values():
    assert redact("abcdef", max_len=0) == ""
    assert redact("abcdef", max_len=-3) == ""
    assert redact("abcdef", max_len=None) == "abcdef"
    assert redact("abcdef", max_len=100) == "abcdef"


ADVERSARIAL = {
    "a-run": "a" * 200_000,
    "eyJ-run": "eyJ" * 66_666,
    "eyJ-dots": "eyJa." * 40_000,
    "eyJ-dot-chain": "eyJ." * 50_000,
    "quote-run": '"' * 200_000,
    "backslash-run": "\\" * 200_000,
    "space-run-after-key": "password" + " " * 200_000,
    "authorization-spaces": "Authorization:" + " " * 200_000,
    "authorization-repeat": "Authorization: " * 13_000,
    "password-open-quote": 'password="' * 20_000,
    "code-single-quote": "code='" * 33_000,
    "code-escaped-quote": 'code="\\' * 40_000,
    "unterminated-then-keys": 'password="' + "password=" * 22_000,
    "bearer-repeat": "Bearer " * 28_000,
    "bearer-long-token": "Bearer " + "a" * 200_000,
    "xox-repeat": "xoxb-" * 40_000,
    "ya29-repeat": "ya29." * 40_000,
    "1//-repeat": "1//" * 66_666,
    "marker-repeat": MARKER * 20_000,
    "key-repeat": "refresh_token=" * 14_000,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_adversarial_input_finishes_well_under_a_second(name):
    text = ADVERSARIAL[name]
    started = time.perf_counter()
    redact(text, ["adversarial-secret"])
    assert time.perf_counter() - started < 1.0


def test_many_overlapping_secret_hits_finish_quickly():
    started = time.perf_counter()
    out = redact("a" * 200_000, ["aaaaaa"])
    assert time.perf_counter() - started < 1.0
    assert out == MARKER


@pytest.mark.parametrize(
    "text",
    [
        f"tokens {SLACK} {YA29} {REFRESH} {GOCSPX} {ANTHROPIC} {JWT}",
        "Authorization: Bearer abc123def and password=hunter22",
        '{"password": "hunter22", "code": "4/0Ab", "n": 1}',
        '{"client_secret": "abcdef123456',
        f"echo {SECRET} and {quote(SECRET, safe='')}",
        "plain text without anything",
        "Authorization: Bearer",
        f"the redacted {MARKER} word",
        "password=]x code=]",
    ],
)
def test_idempotent(text):
    once = redact(text, [SECRET])
    assert redact(once, [SECRET]) == once
    assert redact(redact(once, [SECRET], max_len=40), [SECRET], max_len=40) == redact(
        once, [SECRET], max_len=40
    )


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-21T10:15:30Z run finished",
        "2026-09-21 10:15:30.123456",
        "contact person@example.com or first.last+tag@mail.example.org",
        "id 123e4567-e89b-12d3-a456-426614174000 done",
        "the quick brown fox",
        "a b c 1 22 333",
        "HTTP 429 Too Many Requests, retry in 30s",
        "Collected 12 items from 3 sources in 4.2s",
        "https://example.com/path?page=2&limit=50",
        "error_code=42 status code: 500",
        "codec=h264 passwordless login enabled",
        "ConnectError: All connection attempts failed",
        "eyJ",
        "1//2 of the items",
        "Bearer authentication",
        "Basic configuration failed",
    ],
)
def test_ordinary_text_is_not_mangled(text):
    assert redact(text, [SECRET]) == text


def test_empty_and_marker_only_inputs():
    assert redact("") == ""
    assert redact(MARKER) == MARKER
