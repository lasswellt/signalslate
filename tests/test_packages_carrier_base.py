"""Carrier base: token cache, auth styles, retry mapping, rate limiter, repr hygiene."""
from datetime import datetime

import pytest

from pipeline.packages.carriers import base
from pipeline.packages.carriers.base import (
    CarrierAuthError, CarrierNotFound, CarrierRateLimited, CarrierUnavailable,
    OAuthClientCredentials, RateLimiter, TrackEvent, TrackResult, normalise_status, request_json,
)


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = {} if body is None else body
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeRequests:
    """Stands in for the requests module at base.requests."""
    RequestException = base.requests.RequestException

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)

    def post(self, *args, **kwargs):
        return self._next(*args, **kwargs)

    def request(self, *args, **kwargs):
        return self._next(*args, **kwargs)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(base, "_sleep", recorded.append)
    monkeypatch.setattr(base, "_random", lambda: 0.0)
    return recorded


def stub(monkeypatch, responses):
    fake = FakeRequests(responses)
    monkeypatch.setattr(base, "requests", fake)
    return fake


def test_token_cached_then_refreshed_after_expiry(monkeypatch):
    fake = stub(monkeypatch, [Resp(200, {"access_token": "a", "expires_in": 300}),
                              Resp(200, {"access_token": "b", "expires_in": 300})])
    clock = Clock()
    oauth = OAuthClientCredentials("https://t/token", "id", "sec", clock=clock)
    assert oauth.get_token() == "a"
    clock.now += 239
    assert oauth.get_token() == "a"
    assert len(fake.calls) == 1
    clock.now += 2  # 241s >= 300 - 60
    assert oauth.get_token() == "b"
    assert len(fake.calls) == 2


def test_basic_auth_style(monkeypatch):
    fake = stub(monkeypatch, [Resp(200, {"access_token": "a", "expires_in": 100})])
    OAuthClientCredentials("https://t", "id", "sec", basic_auth=True, scope="s").get_token()
    kwargs = fake.calls[0][1]
    assert kwargs["auth"] == ("id", "sec")
    assert kwargs["data"] == {"grant_type": "client_credentials", "scope": "s"}
    assert kwargs["timeout"] == 15


def test_form_auth_style(monkeypatch):
    fake = stub(monkeypatch, [Resp(200, {"access_token": "a", "expires_in": 100})])
    OAuthClientCredentials("https://t", "id", "sec").get_token()
    kwargs = fake.calls[0][1]
    assert "auth" not in kwargs
    assert kwargs["data"]["client_id"] == "id" and kwargs["data"]["client_secret"] == "sec"


@pytest.mark.parametrize("status", [401, 403])
def test_token_rejection_is_auth_error(monkeypatch, status):
    stub(monkeypatch, [Resp(status)])
    with pytest.raises(CarrierAuthError):
        OAuthClientCredentials("https://t", "id", "sec").get_token()


def test_secret_not_in_repr():
    oauth = OAuthClientCredentials("https://t", "id", "supersecret")
    assert "supersecret" not in repr(oauth)


def test_429_then_200_honours_retry_after(monkeypatch, sleeps):
    fake = stub(monkeypatch, [Resp(429, headers={"Retry-After": "7"}), Resp(200, {"ok": True})])
    assert request_json("GET", "https://x") == {"ok": True}
    assert sleeps == [7.0]
    assert len(fake.calls) == 2


def test_404_not_found_no_retry(monkeypatch, sleeps):
    fake = stub(monkeypatch, [Resp(404)])
    with pytest.raises(CarrierNotFound):
        request_json("GET", "https://x")
    assert len(fake.calls) == 1 and sleeps == []


def test_401_auth_error(monkeypatch):
    stub(monkeypatch, [Resp(401)])
    with pytest.raises(CarrierAuthError):
        request_json("GET", "https://x")


def test_5xx_exhausted_is_unavailable(monkeypatch, sleeps):
    fake = stub(monkeypatch, [Resp(503)] * base.MAX_ATTEMPTS)
    with pytest.raises(CarrierUnavailable):
        request_json("GET", "https://x")
    assert len(fake.calls) == base.MAX_ATTEMPTS
    assert sleeps == [1.0, 2.0, 4.0]


def test_429_exhausted_is_rate_limited(monkeypatch, sleeps):
    stub(monkeypatch, [Resp(429)] * base.MAX_ATTEMPTS)
    with pytest.raises(CarrierRateLimited):
        request_json("GET", "https://x")


def test_rate_limiter_min_interval():
    clock, slept = Clock(), []
    limiter = RateLimiter(min_interval_seconds=5, clock=clock, sleep=slept.append)
    limiter.acquire()
    clock.now += 2
    limiter.acquire()
    assert slept == [3]


def test_rate_limiter_hourly_budget():
    clock = Clock()
    limiter = RateLimiter(max_per_hour=2, clock=clock, sleep=lambda s: None)
    limiter.acquire()
    clock.now += 10
    limiter.acquire()
    with pytest.raises(CarrierRateLimited) as info:
        limiter.acquire()
    assert info.value.retry_after == pytest.approx(3590)
    clock.now += 3590
    limiter.acquire()  # first stamp aged out


def test_track_result_shape_and_status_normalisation():
    event = TrackEvent(datetime(2026, 1, 1), "in_transit", "Departed", None)
    result = TrackResult("1Z", "in_transit", (event,), None, None)
    assert result.events[0].status == "in_transit"
    assert normalise_status("Out for Delivery") == "out_for_delivery"
    assert normalise_status("DELIVERED") == "delivered"
    assert normalise_status("Delivery attempted") == "exception"
    assert normalise_status("Label Created") == "pending"
    assert normalise_status("???") == "unknown"
    assert normalise_status(None) == "unknown"
