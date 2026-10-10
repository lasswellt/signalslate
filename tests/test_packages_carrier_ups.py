"""UPS adapter: parsing, status mapping, typed errors, dormancy. HTTP stubbed at base.requests."""
from datetime import datetime

import pytest

from pipeline.packages.carriers import base, ups
from pipeline.packages.carriers.base import CarrierAuthError, CarrierNotFound

NUMBER = "1Z999AA10123456784"


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
        self.token = token or Resp(200, {"access_token": "tok", "expires_in": 14400})
        self.track = track
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.token

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.track


def act(type_, desc, date, time, city="Louisville", st="KY", country="US"):
    return {"status": {"type": type_, "description": desc, "code": "XX"},
            "location": {"address": {"city": city, "stateProvince": st, "country": country}},
            "date": date, "time": time}


def body(activities, current_type="I", delivery=None, warnings=None):
    pkg = {"activity": activities, "currentStatus": {"type": current_type, "description": "x"}}
    if delivery:
        pkg["deliveryDate"] = delivery
    shipment = {"package": [pkg]}
    if warnings:
        shipment["warnings"] = warnings
    return {"trackResponse": {"shipment": [shipment]}}


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv("UPS_CLIENT_ID", "id")
    monkeypatch.setenv("UPS_CLIENT_SECRET", "secret")
    a = ups.adapter()
    a._limiter = base.RateLimiter(0.0, None)
    return a


def use(monkeypatch, fake):
    monkeypatch.setattr(base, "requests", fake)
    return fake


def test_configured_false_without_keys(monkeypatch):
    assert ups.adapter().configured() is False
    monkeypatch.setenv("UPS_CLIENT_ID", "id")
    assert ups.adapter().configured() is False
    monkeypatch.setenv("UPS_CLIENT_SECRET", "s")
    assert ups.adapter().configured() is True


def test_delivered(adapter, monkeypatch):
    b = body([act("D", "Delivered", "20261003", "141500"), act("I", "Departed", "20261001", "083000")],
             current_type="D", delivery=[{"type": "DEL", "date": "20261003"}])
    fake = use(monkeypatch, FakeRequests(track=Resp(200, b)))
    r = adapter.track(NUMBER)
    assert r.status == "delivered"
    assert r.delivered_at == datetime(2026, 10, 3, 14, 15)
    method, url, kw = fake.calls[-1]
    assert url == f"https://onlinetools.ups.com/api/track/v1/details/{NUMBER}"
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert kw["headers"]["transactionSrc"] == "signalslate"
    assert kw["params"] == {"locale": "en_US", "returnSignature": "false"}
    assert fake.calls[0][2]["auth"] == ("id", "secret")


def test_out_for_delivery_with_eta(adapter, monkeypatch):
    b = body([act("O", "Out For Delivery Today", "20261005", "070000")], current_type="O",
             delivery=[{"type": "SDD", "date": "20261005"}])
    use(monkeypatch, FakeRequests(track=Resp(200, b)))
    r = adapter.track(NUMBER)
    assert r.status == "out_for_delivery"
    assert r.eta == datetime(2026, 10, 5)
    assert r.delivered_at is None


def test_exception(adapter, monkeypatch):
    b = body([act("X", "Delivery attempted", "20261005", "170000")], current_type="X")
    use(monkeypatch, FakeRequests(track=Resp(200, b)))
    assert adapter.track(NUMBER).status == "exception"


def test_not_found_warning(adapter, monkeypatch):
    b = {"trackResponse": {"shipment": [{"warnings": [
        {"code": "TW0001", "message": "Tracking Information Not Found"}]}]}}
    use(monkeypatch, FakeRequests(track=Resp(200, b)))
    with pytest.raises(CarrierNotFound):
        adapter.track(NUMBER)


def test_token_401_is_auth_error(adapter, monkeypatch):
    use(monkeypatch, FakeRequests(token=Resp(401)))
    with pytest.raises(CarrierAuthError):
        adapter.track(NUMBER)


def test_track_404_is_not_found(adapter, monkeypatch):
    use(monkeypatch, FakeRequests(track=Resp(404)))
    with pytest.raises(CarrierNotFound):
        adapter.track(NUMBER)


def test_non_ups_number_spends_no_call(adapter, monkeypatch):
    fake = use(monkeypatch, FakeRequests())
    with pytest.raises(CarrierNotFound):
        adapter.track("9400111899223197428490")
    assert fake.calls == []


def test_home_depot_number_sent_as_is(adapter, monkeypatch):
    b = body([act("I", "Origin Scan", "20261001", "010000")])
    fake = use(monkeypatch, FakeRequests(track=Resp(200, b)))
    assert adapter.track("1R00123456789").status == "in_transit"
    assert fake.calls[-1][1].endswith("/1R00123456789")


def test_events_sorted_ascending_and_location(adapter, monkeypatch):
    b = body([act("I", "Arrived", "20261003", "100000", "Dallas", "TX"),
              act("P", "Pickup", "20261001", "090000", "Reno", "NV"),
              act("M", "Label created", "20260930", "120000", "", "", "US")])
    use(monkeypatch, FakeRequests(track=Resp(200, b)))
    r = adapter.track(NUMBER)
    times = [e.occurred_at for e in r.events]
    assert times == sorted(times)
    assert r.events[0].status == "pending"
    assert r.events[0].location == "US"
    assert r.events[1].location == "Reno, NV, US"
    assert r.events[2].location == "Dallas, TX, US"
