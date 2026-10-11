"""Acceptance and unit tests for pipeline.packages.extract (fixture-driven)."""
import json
from datetime import datetime
from pathlib import Path
from typing import Sequence

import pytest

from pipeline.packages.extract import MailInput, extract

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "packages" / "extraction_cases.json").read_text("utf-8")
)["cases"]
_NOW = datetime(2026, 9, 14, 12, 0, 0)


def _mail(sender: str, subject: str, plain: str, links: Sequence[str] = ()) -> MailInput:
    body = plain + "".join("\n[](%s)" % u for u in links)
    return MailInput(sender=sender, subject=subject, html=None, plain=body, received_at=_NOW)


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_extraction_case(case: dict) -> None:
    exp = case["expect"]
    ships = extract(_mail(case["sender"], case["subject"], case["plain"], case["links"]))
    if "carrier" not in exp and "tracking_number" not in exp:
        # status-only expectation (non-shipping noise): order-only shipments, which
        # inventory.apply_email drops without a status signal.
        assert all(s.tracking_number is None for s in ships), ships
        return
    assert len(ships) == 1, ships
    ship = ships[0]
    assert ship.carrier == exp["carrier"]
    assert ship.tracking_number == exp["tracking_number"]
    if "order_id" in exp:
        assert ship.order_id == exp["order_id"]
    if "confidence" in exp:
        assert ship.confidence == exp["confidence"]
    if "merchant" in exp:
        assert ship.merchant == exp["merchant"]
    if "reference" in exp:
        assert ship.order_id == exp["reference"]  # FedEx "Reference:" lands in order_id
    if exp.get("poll") is False:
        assert ship.carrier not in ("ups", "usps")  # no carrier adapter would poll it


def test_order_labeled_number_is_never_tracking() -> None:
    ships = extract(_mail("shop@example-lab.test", "Order shipped",
                          "Order number: 30000000000000\nThanks"))
    assert [s.tracking_number for s in ships] == [None]
    assert ships[0].order_id == "30000000000000"


def test_phone_label_excludes_number() -> None:
    ships = extract(_mail("shop@example.test", "Hello", "Call 9579235444 now"))
    assert ships == []


def test_shopify_subject_gives_order_id_from_any_sender() -> None:
    ships = extract(_mail("orders@any-store.test",
                          "A shipment from order #555123 is out for delivery", "On its way"))
    assert len(ships) == 1
    assert ships[0].order_id == "555123"
    assert ships[0].tracking_number is None


def test_duplicate_number_in_text_and_link_reported_once() -> None:
    ships = extract(_mail("a@shop.test", "Shipped", "Tracking number: 1Z9X9X9X0326709847",
                          ["https://www.ups.com/track?tracknum=1Z9X9X9X0326709847"]))
    assert [s.tracking_number for s in ships] == ["1Z9X9X9X0326709847"]
