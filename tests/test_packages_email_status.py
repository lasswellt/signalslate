"""Tests for pipeline.packages.email_status.

Assumption: received_at is 2026-09-14T12:00:00 for every fixture case, which anchors year
inference for dates written without a year.
"""
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from pipeline.packages import STATUSES
from pipeline.packages.email_status import StatusSignal, classify_status

RECEIVED = datetime(2026, 9, 14, 12, 0, 0)
_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "packages" / "extraction_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", [c for c in _CASES if "status" in c["expect"]], ids=lambda c: c["name"])
def test_fixture_status_and_eta(case):
    sig = classify_status(case["sender"], case["subject"], case["plain"], RECEIVED)
    expect = case["expect"]
    if expect["status"] is None:
        assert sig is None
        return
    assert sig is not None
    assert sig.status == expect["status"]
    assert sig.status in STATUSES
    assert sig.occurred_at == RECEIVED
    assert len(sig.description) <= 120
    if "eta" in expect:
        assert sig.eta == date.fromisoformat(expect["eta"])


def _c(sender, subject, text="", received=RECEIVED):
    return classify_status(sender, subject, text, received)


@pytest.mark.parametrize("subject,status", [
    ("Thanks for your delivery order, Alex", "pending"),
    ("Thanks for your InHome delivery order", "pending"),
    ("Shipped: Example Item", "in_transit"),
    ("Your package is in transit", "in_transit"),
    ("Out for delivery: Example Item", "out_for_delivery"),
    ("Your package should arrive by 3:01pm", "out_for_delivery"),
    ("Arrived: Your Example Item", "delivered"),
    ("Delivered: Example Item +2 items", "delivered"),
    ("Your order is delayed", "exception"),
    ("Your delivery is delayed", "exception"),
    ("Canceled: delivery of Example Item", "exception"),
])
def test_walmart_subjects(subject, status):
    sig = _c("help@walmart.com", subject)
    assert sig is not None and sig.status == status


def test_walmart_canceled_description():
    assert "canceled" in _c("help@walmart.com", "Canceled: delivery of Item").description


def test_walmart_eta_year_roll():
    sig = _c("help@walmart.com", "Shipped: Item", "Arrives Wed, Jan 6",
             datetime(2026, 12, 20, 9, 0))
    assert sig.eta == date(2027, 1, 6)


def test_walmart_eta_same_year():
    sig = _c("help@walmart.com", "Shipped: Item", "Arrives Wed, Sep 16")
    assert sig.eta == date(2026, 9, 16)


@pytest.mark.parametrize("subject", [
    "Take action: Review your order updates",
    "Reminder for your upcoming order",
    "Review your recent purchase",
    "Your refund is on the way",
    "Tell us in this survey",
])
def test_walmart_noise_none(subject):
    assert _c("help@walmart.com", subject, "Order number: 2000333-33333333") is None


def test_walmart_substitution_body_none():
    assert _c("help@walmart.com", "Order update", "Your order has 2 substitutions") is None


@pytest.mark.parametrize("text,status", [
    ("Your shipment is on the way.", "in_transit"),
    ("Your shipment is scheduled for delivery tomorrow.", "in_transit"),
    ("Your shipment is out for delivery today.", "out_for_delivery"),
    ("Your shipment was delivered.", "delivered"),
])
def test_fedex(text, status):
    sig = _c("TrackingUpdates@fedex.com", "FedEx shipment 753045655424", text)
    assert sig is not None and sig.status == status


def test_fedex_eta():
    sig = _c("TrackingUpdates@fedex.com", "Your shipment is on the way 1",
             "Scheduled delivery date Sat 9/19/2026 Estimated between")
    assert sig.eta == date(2026, 9, 19)


@pytest.mark.parametrize("subject,text,status", [
    ("DHL On Demand Delivery", "Your shipment is On Its Way", "in_transit"),
    ("DHL On Demand Delivery", "Delivery Is Today", "out_for_delivery"),
    ("DHL On Demand Delivery", "ARRIVING SOON", "out_for_delivery"),
    ("DHL On Demand Delivery", "Delivered! has been delivered", "delivered"),
])
def test_dhl(subject, text, status):
    assert _c("NoReply.ODD@dhl.com", subject, text).status == status


@pytest.mark.parametrize("subject", ["Import duties payment required", "Pay duties and taxes", "Payment due for your shipment"])
def test_dhl_payment_none(subject):
    assert _c("NoReply.ODD@dhl.com", subject, "On Its Way") is None


@pytest.mark.parametrize("subject,status", [
    ("Order # W1 Shipped: Your order is on its way", "in_transit"),
    ("Out for Delivery Today", "out_for_delivery"),
    ("We've delivered your order", "delivered"),
    ("Part of your order has been delivered", "delivered"),
    ("Order confirmed", "pending"),
    ("We received your order", "pending"),
])
def test_home_depot(subject, status):
    assert _c("HomeDepotPro@order.homedepot.com", subject).status == status


def test_home_depot_partial_described():
    assert "partial" in _c("a@order.homedepot.com", "Part of your order has been delivered").description


@pytest.mark.parametrize("state,status", [
    ("Dispatched", "in_transit"), ("Out for Delivery", "out_for_delivery"), ("Delivered", "delivered"),
])
def test_sefl(state, status):
    assert _c("batch@freight.test", f"Freight Pro# 100000001 Status: {state}").status == status


@pytest.mark.parametrize("tail,status", [
    ("is on the way", "in_transit"), ("is out for delivery", "out_for_delivery"),
    ("has been delivered", "delivered"),
])
def test_shopify_template(tail, status):
    assert _c("orders@store.test", f"A shipment from order #100 {tail}").status == status


@pytest.mark.parametrize("subject,status", [
    ("Your package is now with its carrier!", "in_transit"),
    ("OUT FOR DELIVERY: your item", "out_for_delivery"),
    ("ORDER DELIVERED: your item", "delivered"),
])
def test_ebay(subject, status):
    assert _c("ebay@ebay.com", subject).status == status


@pytest.mark.parametrize("sender,subject,text", [
    ("x@nextdoor.com", "Your neighbors delivered a package", ""),
    ("shop@store.test", "Item restocked: your order", ""),
    ("shop@store.test", "Rate your order delivered today", ""),
    ("shop@store.test", "Your order refund issued", ""),
    ("shop@store.test", "50% off your next order", "shipped"),
    ("shop@store.test", "Weekly newsletter", "shipped"),
    ("shop@store.test", "Our promise", "never delivered"),
    ("shop@store.test", "Promised but never delivered: your order", ""),
])
def test_noise_none(sender, subject, text):
    assert _c(sender, subject, text) is None


def test_generic_returned_and_exception():
    assert _c("a@b.test", "Your order is being returned to sender").status == "returned"
    assert _c("a@b.test", "Your package delivery exception").status == "exception"


def test_description_never_exceeds_limit_and_omits_body():
    sig = _c("a@b.test", "Your order has shipped " + "x" * 300, "secret-body-text")
    assert len(sig.description) <= 120 and "secret" not in sig.description


def test_returns_frozen_signal():
    sig = _c("help@walmart.com", "Shipped: Item")
    assert isinstance(sig, StatusSignal)
    with pytest.raises(Exception):
        sig.status = "delivered"  # type: ignore[misc]
