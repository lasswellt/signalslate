"""
pipeline.oauth_flows is the security core of browser sign-in: a forged, replayed or foreign callback must
fail, and no failure may echo the code or the pasted URL. The clock is injected; nothing sleeps.
"""
import random
import string
import threading
from datetime import datetime, timedelta
from typing import Any

import pytest

from pipeline import oauth_flows as of
from pipeline.oauth_flows import (
    ExpiredFlow,
    FlowError,
    FlowStore,
    FragmentOnlyResponse,
    InvalidCallbackConfig,
    InvalidPastedUrl,
    NonceMismatch,
    ProviderMismatch,
    UnknownFlow,
    build_callback_url,
    parse_pasted_url,
)

T0 = datetime(2026, 9, 21, 12, 0, 0)
VERIFIER = "verifier-secret-value-0123456789"


def _create(store, *, state="state-1", provider="google", mode="paste_back", now=T0, ttl=900, **kw):
    return store.create(
        provider,
        kw.pop("connection_id", "gmail_work"),
        mode,
        state,
        "http://127.0.0.1:8765",
        {"code_verifier": VERIFIER, "state": state},
        ttl_seconds=ttl,
        now=now,
    )


# ---- FlowStore ----------------------------------------------------------------------------------


def test_create_returns_flow_and_url_safe_nonce_stored_only_hashed():
    store = FlowStore()
    flow, nonce = _create(store)
    assert len(nonce) >= 43  # token_urlsafe(32) is 43 chars: 256 bits
    assert flow.nonce_hash != nonce
    assert nonce not in repr(flow)
    assert flow.expires_at == T0 + timedelta(seconds=900)
    assert flow.created_at == T0
    assert (flow.provider, flow.connection_id, flow.mode) == ("google", "gmail_work", "paste_back")


def test_two_flows_get_distinct_ids_and_nonces():
    store = FlowStore()
    a, na = _create(store, state="a")
    b, nb = _create(store, state="b")
    assert a.flow_id != b.flow_id
    assert na != nb


def test_repr_hides_state_payload_and_nonce_hash():
    flow, _ = _create(FlowStore(), state="the-state-value")
    text = repr(flow)
    assert VERIFIER not in text
    assert "the-state-value" not in text
    assert flow.nonce_hash not in text


def test_consume_by_state_returns_the_record_with_payload():
    store = FlowStore()
    flow, nonce = _create(store)
    got = store.consume(state="state-1", nonce=nonce, provider="google", now=T0 + timedelta(seconds=5))
    assert got == flow
    assert got.payload["code_verifier"] == VERIFIER


def test_consume_by_flow_id():
    store = FlowStore()
    flow, nonce = _create(store)
    assert store.consume(flow_id=flow.flow_id, nonce=nonce, now=T0).flow_id == flow.flow_id


def test_single_use_second_consume_is_unknown():
    store = FlowStore()
    _, nonce = _create(store)
    store.consume(state="state-1", nonce=nonce, now=T0)
    with pytest.raises(UnknownFlow):
        store.consume(state="state-1", nonce=nonce, now=T0)
    assert len(store) == 0


def test_forged_state_is_unknown():
    store = FlowStore()
    _, nonce = _create(store)
    with pytest.raises(UnknownFlow):
        store.consume(state="attacker-invented", nonce=nonce, now=T0)
    assert len(store) == 1


@pytest.mark.parametrize("kwargs", [{}, {"state": ""}, {"flow_id": ""}, {"state": None, "flow_id": None}])
def test_no_selector_is_unknown(kwargs):
    store = FlowStore()
    _, nonce = _create(store)
    with pytest.raises(UnknownFlow):
        store.consume(nonce=nonce, now=T0, **kwargs)


def test_non_ascii_selector_does_not_crash():
    store = FlowStore()
    _, nonce = _create(store)
    with pytest.raises(UnknownFlow):
        store.consume(state="sé中", nonce=nonce, now=T0)


def test_ttl_boundary_with_injected_clock():
    store = FlowStore()
    _, nonce = _create(store, ttl=900)
    # One second before expiry is still good.
    flow = store.consume(state="state-1", nonce=nonce, now=T0 + timedelta(seconds=899))
    assert flow.state == "state-1"

    _, nonce = _create(store, state="state-2", ttl=900)
    with pytest.raises(ExpiredFlow):
        store.consume(state="state-2", nonce=nonce, now=T0 + timedelta(seconds=900))


def test_expired_flow_is_removed_and_then_unknown():
    store = FlowStore()
    _, nonce = _create(store)
    with pytest.raises(ExpiredFlow):
        store.consume(state="state-1", nonce=nonce, now=T0 + timedelta(hours=1))
    assert len(store) == 0
    with pytest.raises(UnknownFlow):
        store.consume(state="state-1", nonce=nonce, now=T0)


def test_expired_beats_a_wrong_nonce():
    store = FlowStore()
    _create(store)
    with pytest.raises(ExpiredFlow):
        store.consume(state="state-1", nonce="wrong", now=T0 + timedelta(hours=1))


def test_wrong_nonce_is_rejected_and_does_not_consume():
    store = FlowStore()
    _, nonce = _create(store)
    for bad in ("wrong-nonce", "", None, nonce + "x", nonce[:-1]):
        with pytest.raises(NonceMismatch):
            store.consume(state="state-1", nonce=bad, now=T0)
    # The victim can still finish: an attacker who knows the state cannot burn the flow.
    assert store.consume(state="state-1", nonce=nonce, now=T0).state == "state-1"


def test_nonce_of_another_flow_does_not_open_this_one():
    store = FlowStore()
    _, nonce_a = _create(store, state="a")
    _create(store, state="b")
    with pytest.raises(NonceMismatch):
        store.consume(state="b", nonce=nonce_a, now=T0)
    assert len(store) == 2


def test_non_string_nonce_is_a_mismatch():
    not_a_string: Any = 12345
    store = FlowStore()
    _create(store)
    with pytest.raises(NonceMismatch):
        store.consume(state="state-1", nonce=not_a_string, now=T0)


def test_provider_mismatch_rejected_and_does_not_consume():
    store = FlowStore()
    _, nonce = _create(store, provider="google")
    with pytest.raises(ProviderMismatch):
        store.consume(state="state-1", nonce=nonce, provider="microsoft", now=T0)
    assert store.consume(state="state-1", nonce=nonce, provider="google", now=T0).provider == "google"


def test_provider_omitted_skips_the_provider_check():
    store = FlowStore()
    _, nonce = _create(store, provider="microsoft")
    assert store.consume(state="state-1", nonce=nonce, now=T0).provider == "microsoft"


def test_eviction_cap_drops_the_oldest():
    store = FlowStore()
    nonces = {}
    for i in range(of.MAX_PENDING + 3):
        _, nonces[i] = _create(store, state=f"s{i}", now=T0 + timedelta(seconds=i))
    assert len(store) == of.MAX_PENDING
    for evicted in range(3):
        with pytest.raises(UnknownFlow):
            store.consume(state=f"s{evicted}", nonce=nonces[evicted], now=T0 + timedelta(seconds=30))
    assert store.consume(state="s3", nonce=nonces[3], now=T0 + timedelta(seconds=30)).state == "s3"


def test_cap_is_twenty():
    assert of.MAX_PENDING == 20


def test_create_purges_expired_records():
    store = FlowStore()
    for i in range(5):
        _create(store, state=f"old{i}", ttl=60)
    assert len(store) == 5
    _create(store, state="fresh", now=T0 + timedelta(minutes=5))
    assert len(store) == 1


def test_consume_purges_other_expired_records_even_when_it_fails():
    store = FlowStore()
    for i in range(4):
        _create(store, state=f"old{i}", ttl=60)
    _, nonce = _create(store, state="live", ttl=900)
    later = T0 + timedelta(minutes=5)
    with pytest.raises(UnknownFlow):
        store.consume(state="nope", nonce=nonce, now=later)
    assert len(store) == 1
    assert store.consume(state="live", nonce=nonce, now=later).state == "live"


@pytest.mark.parametrize(
    "kwargs",
    [{"provider": "github"}, {"mode": "implicit"}, {"state": ""}, {"ttl": 0}, {"ttl": -5}],
)
def test_create_rejects_programming_errors(kwargs):
    with pytest.raises(ValueError):
        _create(FlowStore(), **kwargs)


def test_default_ttl_is_fifteen_minutes_and_clock_defaults_to_utcnow():
    flow, nonce = FlowStore().create("google", "gmail_work", "callback", "st", "https://x.example/cb", {})
    assert flow.expires_at - flow.created_at == timedelta(seconds=900)
    assert flow.created_at.tzinfo is None  # naive UTC, like every stored datetime


def test_concurrent_consume_has_exactly_one_winner():
    store = FlowStore()
    _, nonce = _create(store)
    wins, losses = [], []

    def worker():
        try:
            wins.append(store.consume(state="state-1", nonce=nonce, now=T0))
        except UnknownFlow:
            losses.append(1)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1
    assert len(losses) == 15


# ---- parse_pasted_url ---------------------------------------------------------------------------

CODE = "4/0AVeryLongSecretAuthCode-xyz"


def test_good_loopback_with_port():
    got = parse_pasted_url(f"http://127.0.0.1:8765/?code={CODE}&state=st1&scope=x")
    assert got == {"code": CODE, "state": "st1", "error": None, "error_description": None}


def test_only_the_four_keys_are_returned():
    got = parse_pasted_url("http://127.0.0.1:8765/?code=c&state=s&scope=evil&authuser=0&prompt=consent")
    assert set(got) == {"code", "state", "error", "error_description"}


def test_localhost_and_case_insensitive_host_and_scheme():
    assert parse_pasted_url("http://localhost:1/?code=c&state=s")["code"] == "c"
    assert parse_pasted_url("HTTP://LocalHost:8765/cb?code=c&state=s")["state"] == "s"


def test_surrounding_whitespace_is_trimmed():
    assert parse_pasted_url("  http://127.0.0.1:8765/?code=c&state=s\n")["code"] == "c"


def test_percent_encoded_values_are_decoded():
    assert parse_pasted_url("http://127.0.0.1/?code=4%2F0Abc&state=s")["code"] == "4/0Abc"


def test_error_response():
    got = parse_pasted_url("http://127.0.0.1:8765/?error=access_denied&error_description=User+said+no&state=st1")
    assert got == {"code": None, "state": "st1", "error": "access_denied", "error_description": "User said no"}


def test_custom_allowed_hosts():
    url = "http://example.org:80/?code=c&state=s"
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(url)
    assert parse_pasted_url(url, allowed_hosts=("example.org",))["code"] == "c"
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url("http://127.0.0.1/?code=c&state=s", allowed_hosts=("example.org",))


@pytest.mark.parametrize(
    "url",
    [
        f"https://127.0.0.1:8765/?code={CODE}&state=s",
        f"https://localhost/?code={CODE}&state=s",
        f"ftp://127.0.0.1/?code={CODE}&state=s",
        f"//127.0.0.1/?code={CODE}&state=s",
        f"127.0.0.1:8765/?code={CODE}&state=s",
        f"javascript://127.0.0.1/%0a?code={CODE}&state=s",
    ],
    ids=["https-ip", "https-localhost", "ftp", "no-scheme-slashes", "no-scheme", "javascript"],
)
def test_wrong_scheme_rejected(url):
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://evil.example/?code={CODE}&state=s",
        f"http://127.0.0.1.evil.example/?code={CODE}&state=s",
        f"http://localhost.evil.example/?code={CODE}&state=s",
        f"http://evil-localhost/?code={CODE}&state=s",
        f"http://127.0.0.2:8765/?code={CODE}&state=s",
        f"http://0.0.0.0/?code={CODE}&state=s",
        f"http://[::1]:8765/?code={CODE}&state=s",
        f"http://127.0.0.1./?code={CODE}&state=s",
        f"http://2130706433/?code={CODE}&state=s",
        f"http://127.0.0.1%2f.evil.example/?code={CODE}&state=s",
        f"http://evil.example/?x=http://127.0.0.1&code={CODE}&state=s",
        f"http://evil.example/127.0.0.1?code={CODE}&state=s",
        f"http://evil.example#127.0.0.1?code={CODE}&state=s",
    ],
)
def test_other_host_rejected(url):
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://127.0.0.1@evil.example/?code={CODE}&state=s",
        f"http://127.0.0.1:8765@evil.example/?code={CODE}&state=s",
        f"http://user:pw@127.0.0.1:8765/?code={CODE}&state=s",
        f"http://@127.0.0.1:8765/?code={CODE}&state=s",
        f"http://evil.example\\@127.0.0.1/?code={CODE}&state=s",
        f"http://127.0.0.1:8765\\.evil.example/?code={CODE}&state=s",
    ],
    ids=["userinfo-trick", "userinfo-port-trick", "user-pass", "empty-userinfo", "backslash-1", "backslash-2"],
)
def test_userinfo_and_backslash_tricks_rejected(url):
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:99999/?code=c&state=s",
        "http://127.0.0.1:abc/?code=c&state=s",
        "http://127.0.0.1:8765/?code=c &state=s",
        "http://127.0.0.1:8765/?code=c\r\nHost:evil&state=s",
        "http://127.0.0.1:8765/?code=c\x00&state=s",
        "http://[::1/?code=c&state=s",
    ],
    ids=["port-range", "port-text", "space", "crlf", "nul", "bad-ipv6"],
)
def test_malformed_urls_rejected(url):
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(url)


def test_oversize_rejected_at_the_boundary():
    prefix = "http://127.0.0.1:8765/?state=s&code="
    ok = prefix + "a" * (of.MAX_URL_LENGTH - len(prefix))
    assert len(ok) == 4096
    assert parse_pasted_url(ok)["code"] == "a" * of.MAX_FIELD_LENGTH  # capped, not rejected
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(ok + "a")


def test_each_returned_field_is_capped_at_2kb():
    long_desc = "d" * 3000
    got = parse_pasted_url(f"http://127.0.0.1/?error=x&state=s&error_description={long_desc}")
    assert got["error_description"] == "d" * 2048


@pytest.mark.parametrize("url", ["", "   ", None, 123, b"http://127.0.0.1/?code=c&state=s"])
def test_empty_and_non_string_rejected(url):
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8765/", "http://127.0.0.1:8765/?", "http://127.0.0.1:8765/cb", "http://127.0.0.1:8765/?scope=x"],
)
def test_missing_query_or_code_rejected(url):
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://127.0.0.1:8765/?code={CODE}",
        f"http://127.0.0.1:8765/?code={CODE}&state=",
        "http://127.0.0.1:8765/?error=access_denied",
    ],
)
def test_missing_state_rejected(url):
    with pytest.raises(InvalidPastedUrl) as info:
        parse_pasted_url(url)
    assert not isinstance(info.value, FragmentOnlyResponse)


@pytest.mark.parametrize(
    "url",
    [
        f"http://127.0.0.1:8765/#code={CODE}&state=s",
        f"http://127.0.0.1:8765/cb#state=s&code={CODE}",
        "http://127.0.0.1:8765/#error=access_denied&state=s",
        f"http://127.0.0.1:8765/?scope=x#code={CODE}&state=s",
    ],
    ids=["fragment-only", "path-and-fragment", "fragment-error", "unrelated-query-then-fragment"],
)
def test_fragment_only_response_has_a_specific_error(url):
    with pytest.raises(FragmentOnlyResponse) as info:
        parse_pasted_url(url)
    assert "query" in str(info.value)
    assert CODE not in str(info.value)
    # It is still a FlowError, so a generic handler catches it.
    assert isinstance(info.value, FlowError)


@pytest.mark.parametrize(
    "url",
    [
        f"http://127.0.0.1:8765/?code={CODE}&code=other&state=s",
        f"http://127.0.0.1:8765/?code={CODE}&state=a&state=b",
    ],
)
def test_repeated_parameter_rejected(url):
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(url)


def test_too_many_query_fields_rejected():
    query = "&".join(f"k{i}=v" for i in range(200))
    with pytest.raises(InvalidPastedUrl):
        parse_pasted_url(f"http://127.0.0.1:8765/?code=c&state=s&{query}")


def test_parse_never_touches_the_network(monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("network access")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    assert parse_pasted_url("http://localhost:8765/?code=c&state=s")["code"] == "c"


# ---- error messages never leak ------------------------------------------------------------------


def _messages_for(url):
    try:
        parse_pasted_url(url)
    except FlowError as exc:
        return [str(exc), repr(exc), str(exc.args), exc.__class__.__name__]
    return []


def test_flow_error_messages_never_include_the_code_or_the_url():
    rng = random.Random(1234)
    alphabet = string.ascii_letters + string.digits + "-_"
    hosts = ["127.0.0.1", "localhost", "evil.example", "127.0.0.1@evil.example", "127.0.0.1.evil.example"]
    schemes = ["http", "https", "ftp"]
    checked = 0
    for _ in range(400):
        code = "c0de" + "".join(rng.choice(alphabet) for _ in range(rng.randint(8, 60)))
        state = "st4te" + "".join(rng.choice(alphabet) for _ in range(rng.randint(4, 20)))
        host = rng.choice(hosts)
        scheme = rng.choice(schemes)
        port = rng.choice(["", ":8765", ":99999"])
        variant = rng.choice(["query", "fragment", "noquery", "nostate", "dup", "good"])
        base = f"{scheme}://{host}{port}/"
        url = {
            "query": f"{base}?code={code}&state={state}",
            "fragment": f"{base}#code={code}&state={state}",
            "noquery": f"{base}{code}",
            "nostate": f"{base}?code={code}",
            "dup": f"{base}?code={code}&code={code}x&state={state}",
            "good": f"{base}?code={code}&state={state}",
        }[variant]
        for text in _messages_for(url):
            checked += 1
            assert code not in text
            assert state not in text
            assert url not in text
    assert checked > 100


def test_store_and_callback_errors_never_include_selectors():
    store = FlowStore()
    _, nonce = _create(store, state="the-real-state")
    secrets_seen = ["forged-state-abc", "wrong-nonce-xyz", "https://user:pw@x.example", nonce]
    attempts = [
        lambda: store.consume(state="forged-state-abc", nonce=nonce, now=T0),
        lambda: store.consume(state="the-real-state", nonce="wrong-nonce-xyz", now=T0),
        lambda: store.consume(state="the-real-state", nonce=nonce, provider="microsoft", now=T0),
        lambda: store.consume(state="the-real-state", nonce=nonce, now=T0 + timedelta(days=1)),
        lambda: build_callback_url("https://user:pw@x.example", "google"),
    ]
    for attempt in attempts:
        with pytest.raises(FlowError) as info:
            attempt()
        text = " ".join([str(info.value), repr(info.value)])
        for secret in secrets_seen[:3]:
            assert secret not in text
        assert nonce not in text and "the-real-state" not in text


def test_flow_errors_take_no_arguments():
    extra: Any = ("code=secret",)
    for cls in (FlowError, UnknownFlow, ExpiredFlow, NonceMismatch, ProviderMismatch, InvalidPastedUrl, FragmentOnlyResponse):
        with pytest.raises(TypeError):
            cls(*extra)
        assert cls().args == (cls.message,)


# ---- build_callback_url -------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_build_callback_url(provider):
    assert build_callback_url("https://ss.example.com", provider) == f"https://ss.example.com/api/oauth/callback/{provider}"


def test_build_callback_url_normalizes_trailing_slash_and_keeps_port_and_prefix():
    assert build_callback_url("https://ss.example.com/", "google") == "https://ss.example.com/api/oauth/callback/google"
    assert build_callback_url("https://ss.example.com:8443", "microsoft") == "https://ss.example.com:8443/api/oauth/callback/microsoft"
    assert build_callback_url("https://ss.example.com/app/", "google") == "https://ss.example.com/app/api/oauth/callback/google"


@pytest.mark.parametrize(
    "base",
    [
        "http://ss.example.com",
        "http://127.0.0.1:8000",
        "ss.example.com",
        "https://",
        "",
        "ftp://ss.example.com",
        "https://user:pw@ss.example.com",
        "https://ss.example.com?x=1",
        "https://ss.example.com#frag",
        "https://ss.example.com:notaport",
        None,
    ],
)
def test_build_callback_url_rejects_bad_base(base):
    with pytest.raises(InvalidCallbackConfig):
        build_callback_url(base, "google")


@pytest.mark.parametrize("provider", ["github", "", "Google", "google/../x", None])
def test_build_callback_url_rejects_bad_provider(provider):
    with pytest.raises(InvalidCallbackConfig):
        build_callback_url("https://ss.example.com", provider)
