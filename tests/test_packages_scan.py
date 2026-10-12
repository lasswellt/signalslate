"""
Tests for pipeline.packages.scan. No network: requests is stubbed at pipeline.collectors.gmail's own
import site (as tests/test_collectors.py does) and the health token call is replaced. Mails are built
from the synthetic extraction_cases.json fixtures, base64url-encoded like Gmail returns them.
"""
import base64
import html as htmllib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db, health  # noqa: E402
from pipeline.collectors import gmail  # noqa: E402
from pipeline.packages import scan  # noqa: E402

CASES = {c["name"]: c for c in json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "packages" / "extraction_cases.json").read_text())["cases"]}
NOW = datetime(2026, 10, 10, 12, 0, 0)
BODY_SENTENCE = "Zebra quokka marmalade confidential sentence"


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.headers = {}
        self.text = ""

    def json(self):
        return self._json


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(mid: str, case_name: str, when: datetime, as_html: bool = False) -> dict:
    case = CASES[case_name]
    plain = case["plain"] + "\n" + BODY_SENTENCE
    if as_html:
        anchors = "".join(f'<a href="{htmllib.escape(u)}">link</a>' for u in case.get("links", []))
        part = {"mimeType": "text/html", "body": {"data": _b64(f"<html><body><p>{htmllib.escape(plain)}</p>{anchors}</body></html>")}}
    else:
        part = {"mimeType": "text/plain", "body": {"data": _b64(plain)}}
    millis = int((when - datetime(1970, 1, 1)).total_seconds() * 1000)
    return {
        "id": mid, "threadId": f"t-{mid}", "internalDate": str(millis),
        "payload": {"mimeType": "multipart/alternative", "headers": [
            {"name": "From", "value": case["sender"]}, {"name": "Subject", "value": case["subject"]}],
            "parts": [part]},
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(gmail, "_sleep", lambda s: None)
    monkeypatch.setattr(health, "gmail_accounts", lambda: {"main": "rt"})
    monkeypatch.setattr(health, "gmail_token_response", lambda label: {"access_token": f"tok-{label}"})
    state = {"messages": {}, "list_calls": [], "fail_tokens": set()}

    def fake_get(url, headers=None, params=None, timeout=None):
        token = headers["Authorization"].split()[-1]
        if token in state["fail_tokens"]:
            raise gmail.requests.ConnectionError("down")
        if url.endswith("/messages"):
            state["list_calls"].append({"token": token, **(params or {})})
            return FakeResponse({"messages": [{"id": m, "threadId": f"t-{m}"} for m in state["messages"]]})
        return FakeResponse(state["messages"][url.rsplit("/", 1)[1]])

    monkeypatch.setattr(gmail.requests, "get", fake_get)
    state["engine"] = engine
    return state


def _seed(env):
    env["messages"] = {
        "m1": _message("m1", "walmart_fulfilled_by_walmart_shipped", NOW - timedelta(days=3)),
        "m2": _message("m2", "fedex_delivery_manager_on_the_way", NOW - timedelta(days=2)),
        "m3": _message("m3", "narvar_link_ups", NOW - timedelta(days=1), as_html=True),
        "m4": _message("m4", "walmart_non_shipping_noise", NOW - timedelta(hours=5)),
    }


def test_query_contains_purchases_category():
    assert "category:purchases" in scan.SCAN_QUERY


def test_scan_creates_packages_and_cursor(env):
    _seed(env)
    results = scan.scan_all(NOW)
    assert [(r.label, r.errors) for r in results] == [("main", [])]
    assert results[0].scanned == 4
    with db.get_session() as s:
        numbers = {p.tracking_number for p in s.exec(select(db.Package)).all()}
        cursor = s.get(db.SourceCursor, "packages_gmail_main")
    assert {"55511014564076268801", "753045655424", "1Z9X9X9X0326709847"} <= numbers
    assert results[0].packages_touched >= 3
    assert cursor.last_success_at == NOW - timedelta(hours=5)


def test_first_run_uses_scan_days_then_cursor_minus_one_day(env):
    _seed(env)
    scan.scan_all(NOW)
    first = env["list_calls"][0]["q"]
    assert first.endswith(f"after:{gmail.to_epoch_seconds(NOW - timedelta(days=60))}")
    assert first.startswith(scan.SCAN_QUERY)
    with db.get_session() as s:
        stored = s.get(db.SourceCursor, "packages_gmail_main").last_success_at
    scan.scan_all(NOW + timedelta(hours=1))
    second = env["list_calls"][1]["q"]
    assert second.endswith(f"after:{gmail.to_epoch_seconds(stored - timedelta(days=1))}")


def test_rescan_is_idempotent(env):
    _seed(env)
    scan.scan_all(NOW)
    with db.get_session() as s:
        before = (len(s.exec(select(db.PackageEmail)).all()), len(s.exec(select(db.PackageEvent)).all()),
                  len(s.exec(select(db.Package)).all()))
    scan.scan_all(NOW + timedelta(hours=1))
    with db.get_session() as s:
        after = (len(s.exec(select(db.PackageEmail)).all()), len(s.exec(select(db.PackageEvent)).all()),
                 len(s.exec(select(db.Package)).all()))
    assert before == after and before[0] >= 3


def test_failing_account_does_not_stop_next(env, monkeypatch):
    _seed(env)
    monkeypatch.setattr(health, "gmail_accounts", lambda: {"bad": "rt1", "good": "rt2"})
    env["fail_tokens"].add("tok-bad")
    results = scan.scan_all(NOW)
    by = {r.label: r for r in results}
    assert by["bad"].errors == ["GmailError"]
    assert by["good"].errors == [] and by["good"].scanned == 4
    with db.get_session() as s:
        assert s.get(db.SourceCursor, "packages_gmail_bad") is None
        assert s.get(db.SourceCursor, "packages_gmail_good") is not None


def test_token_failure_is_recorded_by_class_name(env, monkeypatch):
    def boom(label):
        raise health.GmailAuthError("invalid_grant secret detail")

    monkeypatch.setattr(health, "gmail_token_response", boom)
    results = scan.scan_all(NOW)
    assert results[0].errors == ["GmailAuthError"]


def test_no_table_stores_mail_body(env):
    _seed(env)
    scan.scan_all(NOW)
    with db.get_session() as s:
        rows = [*s.exec(select(db.Package)).all(), *s.exec(select(db.PackageEvent)).all(),
                *s.exec(select(db.PackageEmail)).all(), *s.exec(select(db.SourceCursor)).all()]
    assert rows
    for row in rows:
        assert BODY_SENTENCE not in json.dumps(row.model_dump(), default=str)
