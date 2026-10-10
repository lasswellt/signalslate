"""
End-to-end package-tracking flow through the real pipeline and API modules.

Design decisions:
- Only true externals are stubbed: Gmail HTTP (pipeline.collectors.gmail.requests), the Gmail token
  call, and carrier HTTP (pipeline.packages.carriers.base.requests). scan, inventory, poll, the
  digest collector and the FastAPI routes are all real, over a temp sqlite engine.
- Mails are built from the synthetic extraction_cases.json fixtures (plus one custom USPS mail,
  because the fixture USPS case is already delivered and a delivered parcel never regresses).
- Privacy is asserted over every table in SQLModel.metadata: distinctive body sentences must not
  appear in any cell, and no body/html/plain column may exist on the package tables.
"""
import base64
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.main import app  # noqa: E402
from api.routers import packages as packages_router  # noqa: E402
from pipeline import db, health  # noqa: E402
from pipeline.collectors import gmail  # noqa: E402
from pipeline.collectors import packages as packages_collector  # noqa: E402
from pipeline.packages import poll, scan  # noqa: E402
from pipeline.packages.carriers import base  # noqa: E402

CASES = {c["name"]: c for c in json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "packages" / "extraction_cases.json").read_text())["cases"]}
NOW = datetime(2026, 10, 10, 12, 0, 0)
UPS_NUMBER = "1Z9X9X9X0326709847"
USPS_NUMBER = "9405511899223197428490"
USPS_SENTENCE = "Quokka marmalade confidential parcel sentence"
USPS_PLAIN = f"Your package is on the way.\nUSPS Tracking Number: {USPS_NUMBER}\n{USPS_SENTENCE}"
FIXTURE_CASES = ("walmart_fulfilled_by_walmart_shipped", "fedex_delivery_manager_on_the_way",
                 "narvar_link_ups", "shopify_yunexpress", "merchant_link_only_no_number")


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.headers = {}
        self.text = ""

    def json(self):
        return self._json


class FakeCarrierRequests:
    """Stands in for the requests module imported by carriers.base: OAuth token + tracking answers."""
    RequestException = base.requests.RequestException

    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        return FakeResponse({"access_token": "tok", "expires_in": 3600})

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if "onlinetools.ups.com" in url:
            assert url.endswith(UPS_NUMBER)
            return FakeResponse({"trackResponse": {"shipment": [{"package": [{
                "currentStatus": {"type": "D", "description": "Delivered"},
                "activity": [
                    {"status": {"type": "D", "description": "Delivered", "code": "FS"},
                     "location": {"address": {"city": "Austin", "stateProvince": "TX", "country": "US"}},
                     "date": "20261010", "time": "103000"},
                    {"status": {"type": "I", "description": "Departed", "code": "DP"},
                     "location": {"address": {"city": "Louisville", "stateProvince": "KY", "country": "US"}},
                     "date": "20261009", "time": "083000"}],
                "deliveryDate": [{"type": "DEL", "date": "20261010"}]}]}]}})
        assert USPS_NUMBER in url
        return FakeResponse({
            "trackingNumber": USPS_NUMBER, "statusCategory": "Out for Delivery", "status": "Out for Delivery",
            "expectedDeliveryDate": "2026-10-10",
            "trackingEvents": [{"eventType": "Out for Delivery", "eventTimestamp": "2026-10-10T09:00:00Z",
                                "eventCity": "Austin", "eventState": "TX", "eventCountry": "US",
                                "eventCode": "OF"}]})


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _message(mid: str, sender: str, subject: str, plain: str, when: datetime) -> dict:
    millis = int((when - datetime(1970, 1, 1)).total_seconds() * 1000)
    html = f"<html><body><p>{plain}</p></body></html>".replace("\n", "<br>")
    return {"id": mid, "threadId": f"t-{mid}", "internalDate": str(millis),
            "payload": {"mimeType": "multipart/alternative",
                        "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
                        "parts": [{"mimeType": "text/plain", "body": {"data": _b64(plain)}},
                                  {"mimeType": "text/html", "body": {"data": _b64(html)}}]}}


def _bodies() -> list[str]:
    return [CASES[n]["plain"] for n in FIXTURE_CASES] + [USPS_PLAIN]


@pytest.fixture
def world(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'e2e.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(gmail, "_sleep", lambda s: None)
    monkeypatch.setattr(health, "gmail_accounts", lambda: {"main": "rt"})
    monkeypatch.setattr(health, "gmail_token_response", lambda label: {"access_token": "tok-main"})
    messages = {}
    for i, name in enumerate(FIXTURE_CASES):
        case = CASES[name]
        messages[f"m{i}"] = _message(f"m{i}", case["sender"], case["subject"], case["plain"],
                                     NOW - timedelta(days=3, hours=i))
    messages["m-usps"] = _message("m-usps", "orders@example-gifts.test", "Your order has shipped",
                                  USPS_PLAIN, NOW - timedelta(days=1))

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/messages"):
            return FakeResponse({"messages": [{"id": m, "threadId": f"t-{m}"} for m in messages]})
        return FakeResponse(messages[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(gmail.requests, "get", fake_get)
    carrier_http = FakeCarrierRequests()
    monkeypatch.setattr(base, "requests", carrier_http)
    monkeypatch.setattr(packages_router, "utcnow", lambda: NOW)
    return {"engine": engine, "http": carrier_http}


def _by_number(session) -> dict:
    return {p.tracking_number: p for p in session.exec(select(db.Package)).all() if p.tracking_number}


def test_scan_poll_digest_api_and_privacy(world, monkeypatch):
    # Step 1: scan creates packages from the fixture mails.
    results = scan.scan_all(NOW)
    assert [(r.label, r.errors, r.scanned) for r in results] == [("main", [], 6)]
    with db.get_session() as s:
        packages = s.exec(select(db.Package)).all()
        by_number = _by_number(s)
        assert {"55511014564076268801", "753045655424", UPS_NUMBER, "YT0000000000000001",
                USPS_NUMBER} <= set(by_number)
        assert by_number["55511014564076268801"].carrier == "walmart"
        assert by_number[UPS_NUMBER].carrier == "ups" and by_number[USPS_NUMBER].carrier == "usps"
        assert any(p.tracking_number is None and p.order_id == "2222222" for p in packages)
        assert by_number[UPS_NUMBER].status == "in_transit"
        emails = s.exec(select(db.PackageEmail)).all()
        assert len(emails) >= 5
        assert all(e.message_id and e.account_label == "main" for e in emails)

    # Step 2: poll with carrier HTTP stubbed at base.requests.
    for key in ("UPS_CLIENT_ID", "UPS_CLIENT_SECRET", "USPS_CLIENT_ID", "USPS_CLIENT_SECRET"):
        monkeypatch.setenv(key, "x")
    polled = poll.refresh_due(NOW)
    assert polled.errors == {} and polled.polled == 2 and polled.changed == 2
    with db.get_session() as s:
        by_number = _by_number(s)
        ups, usps = by_number[UPS_NUMBER], by_number[USPS_NUMBER]
        assert ups.status == "delivered" and ups.delivered_at is not None
        assert usps.status == "out_for_delivery"
        events = s.exec(select(db.PackageEvent).where(db.PackageEvent.package_id == ups.id)).all()
        assert len(events) >= 2
        ups_id, usps_id = ups.id, usps.id
    assert {url.split("/")[2] for _, url in world["http"].calls} == {
        "onlinetools.ups.com", "apis.usps.com"}

    # Step 3: digest collector.
    collected = packages_collector.collect_packages(NOW - timedelta(days=1), NOW + timedelta(hours=1))
    assert collected.source == "packages" and collected.status == "ok"
    ids = {i.external_id for i in collected.items}
    assert {f"package:{ups_id}:delivered", f"package:{usps_id}:out_for_delivery"} <= ids
    assert all(i.item_type == "package" for i in collected.items)

    # Step 4: API.
    client = TestClient(app)
    listed = client.get("/api/packages")
    assert listed.status_code == 200
    listed_numbers = {row["tracking_number"] for row in listed.json()}
    assert {UPS_NUMBER, USPS_NUMBER} <= listed_numbers
    summary = client.get("/api/packages/summary").json()
    assert summary["delivered_today"] == 1
    assert summary["out_for_delivery_today"] == 1

    # Step 5: privacy.
    for model in (db.Package, db.PackageEvent, db.PackageEmail):
        assert not {"body", "html", "plain"} & {c.name for c in model.__table__.columns}
    sentences = [USPS_SENTENCE]
    for plain in _bodies():
        for line in plain.splitlines():
            line = line.strip()
            if len(line) >= 30 and not any(ch.isdigit() for ch in line):
                sentences.append(line)
    assert len(sentences) >= 4
    with world["engine"].connect() as conn:
        for table in SQLModel.metadata.sorted_tables:
            for row in conn.execute(text(f'SELECT * FROM "{table.name}"')).fetchall():
                cells = " ".join(str(c) for c in row)
                for sentence in sentences:
                    assert sentence not in cells, f"{table.name} stores body text"
