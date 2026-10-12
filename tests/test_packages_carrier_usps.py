"""USPS adapter: parsing, status mapping, number normalisation, typed errors, dormancy, budget. HTTP stubbed at base.requests."""
from datetime import datetime

import pytest

from pipeline.packages.carriers import base, usps
from pipeline.packages.carriers.base import CarrierAuthError, CarrierNotFound, CarrierRateLimited

NUMBER = "9405511899223197428490"
PREFIXED = "420300609405511899223197428490"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {} if body is None else body
        self.headers = {}

    def json(self):
        return self._body


class FakeRequests:
    RequestException = base.requests.RequestException

    def __init__(self, token=None, track=None):
        self.token = token or Resp(200, {"access_token": "tok", "expires_in": 3600})
        self.track = track
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.token

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.track


def ev(type_, ts, city="Memphis", st="TN", country="US"):
    return {"eventType": type_, "eventTimestamp": ts, "eventCity": city, "eventState": st,
            "eventCountry": country, "eventCode": "XX"}


def body(category, events, expected=None, status="s"):
    b = {"trackingNumber": NUMBER, "statusCategory": category, "status": status,
         "trackingEvents": events}
    if expected:
        b["expectedDeliveryDate"] = expected
    return b


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv("USPS_CLIENT_ID", "id")
    monkeypatch.setenv("USPS_CLIENT_SECRET", "secret")
    a = usps.adapter()
    a._limiter = base.RateLimiter(0.0, None)
    return a


def use(monkeypatch, fake):
    monkeypatch.setattr(base, "requests", fake)
    return fake


def test_configured_false_without_keys(monkeypatch):
    assert usps.adapter().configured() is False
    monkeypatch.setenv("USPS_CLIENT_ID", "id")
    assert usps.adapter().configured() is False
    monkeypatch.setenv("USPS_CLIENT_SECRET", "s")
    assert usps.adapter().configured() is True


def test_delivered(adapter, monkeypatch):
    b = body("Delivered", [ev("Delivered, Front Door", "2026-10-03T14:15:00-04:00"),
                           ev("Departed USPS Facility", "2026-10-01T08:30:00-04:00")])
    fake = use(monkeypatch, FakeRequests(track=Resp(200, b)))
    r = adapter.track(NUMBER)
    assert r.status == "delivered"
    assert r.delivered_at == datetime(2026, 10, 3, 18, 15)
    assert [e.status for e in r.events] == ["in_transit", "delivered"]
    assert r.events[0].location == "Memphis, TN, US"
    method, url, kwargs = fake.calls[-1]
    assert url == usps.TRACK_URL.format(number=NUMBER)
    assert kwargs["params"] == {"expand": "DETAIL"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    post = fake.calls[0]
    assert post[1] == usps.TOKEN_URL and post[2]["data"]["client_id"] == "id"


def test_out_for_delivery_with_eta(adapter, monkeypatch):
    b = body("Out for Delivery", [ev("Out for Delivery", "2026-10-04T07:00:00-05:00")],
             expected="2026-10-04")
    use(monkeypatch, FakeRequests(track=Resp(200, b)))
    r = adapter.track(NUMBER)
    assert r.status == "out_for_delivery"
    assert r.eta == datetime(2026, 10, 4)
    assert r.delivered_at is None


def test_alert_is_exception(adapter, monkeypatch):
    b = body("Alert", [ev("Delivery Attempt - Unable to Deliver", "2026-10-04T12:00:00Z")])
    use(monkeypatch, FakeRequests(track=Resp(200, b)))
    r = adapter.track(NUMBER)
    assert r.status == "exception"
    assert r.events[0].status == "exception"


def test_return_to_sender(adapter, monkeypatch):
    b = body("Return to Sender", [ev("Return to Sender", "2026-10-05T12:00:00Z")])
    use(monkeypatch, FakeRequests(track=Resp(200, b)))
    assert adapter.track(NUMBER).status == "returned"


def test_pre_shipment_and_in_transit(adapter, monkeypatch):
    use(monkeypatch, FakeRequests(track=Resp(200, body("Pre-Shipment", []))))
    assert adapter.track(NUMBER).status == "pending"
    use(monkeypatch, FakeRequests(track=Resp(200, body("In Transit", []))))
    assert adapter.track(NUMBER).status == "in_transit"


def test_420_zip_prefix_stripped(adapter, monkeypatch):
    fake = use(monkeypatch, FakeRequests(track=Resp(200, body("In Transit", []))))
    r = adapter.track(PREFIXED)
    assert fake.calls[-1][1] == usps.TRACK_URL.format(number=NUMBER)
    assert r.tracking_number == NUMBER


def test_420_zip9_prefix_and_spaces_stripped():
    assert usps.normalise_number("420 300609300 " + NUMBER) == NUMBER


def test_non_usps_rejected_without_http(adapter, monkeypatch):
    fake = use(monkeypatch, FakeRequests(track=Resp(200, {})))
    with pytest.raises(CarrierNotFound):
        adapter.track("1Z999AA10123456784")
    assert fake.calls == []


def test_token_401_is_auth_error(adapter, monkeypatch):
    use(monkeypatch, FakeRequests(token=Resp(401), track=Resp(200, {})))
    with pytest.raises(CarrierAuthError):
        adapter.track(NUMBER)


def test_404_is_not_found(adapter, monkeypatch):
    use(monkeypatch, FakeRequests(track=Resp(404)))
    with pytest.raises(CarrierNotFound):
        adapter.track(NUMBER)


def test_error_body_is_not_found(adapter, monkeypatch):
    use(monkeypatch, FakeRequests(track=Resp(200, {"error": {"message": "not found"}})))
    with pytest.raises(CarrierNotFound):
        adapter.track(NUMBER)


def test_offset_timestamps_naive_utc():
    assert usps._parse_timestamp("2026-10-03T14:15:00-04:00") == datetime(2026, 10, 3, 18, 15)
    assert usps._parse_timestamp("2026-10-03T01:00:00+09:00") == datetime(2026, 10, 2, 16, 0)
    assert usps._parse_timestamp("2026-10-03T01:00:00Z") == datetime(2026, 10, 3, 1, 0)
    assert usps._parse_timestamp("garbage") is None


def test_hourly_budget_enforced(monkeypatch):
    monkeypatch.setenv("USPS_CLIENT_ID", "id")
    monkeypatch.setenv("USPS_CLIENT_SECRET", "secret")
    a = usps.adapter()
    assert a._limiter.max_per_hour == usps.DEFAULT_HOURLY_BUDGET == 60
    a._limiter = base.RateLimiter(0.0, 2)
    use(monkeypatch, FakeRequests(track=Resp(200, body("In Transit", []))))
    a.track(NUMBER)
    a.track(NUMBER)
    with pytest.raises(CarrierRateLimited):
        a.track(NUMBER)
