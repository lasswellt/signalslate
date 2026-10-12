"""Tests for pipeline.packages.links carrier deep links."""
import pytest

from pipeline.packages.links import tracking_url


@pytest.mark.parametrize(
    "carrier,number,host",
    [
        ("ups", "1Z999AA10123456784", "ups.com"),
        ("usps", "9400111899223197428490", "usps.com"),
        ("fedex", "986578788855", "fedex.com"),
        ("dhl", "3318810025", "dhl.com"),
    ],
)
def test_jkeen_templates_https_with_number(carrier, number, host):
    url = tracking_url(carrier, number)
    assert url.startswith("https://") and host in url and number in url


def test_format_id_template_preferred():
    assert "fedextrack" in tracking_url("fedex_12", "986578788855")


def test_number_is_url_encoded():
    url = tracking_url("ups", "1Z 99&x=1#/")
    assert "1Z%2099%26x%3D1%23%2F" in url
    assert "&x=1" not in url


def test_walmart_override():
    assert tracking_url("walmart", None, order_id="2000 1/2") == "https://www.walmart.com/orders/2000%201%2F2"
    assert tracking_url("walmart", None) == "https://www.walmart.com/orders"


def test_sefl_override():
    assert tracking_url("sefl", "123456789") == "https://www.sefl.com/Tracing/index.jsp?Type=PN&RefNum1=123456789"


def test_amazon_tba_override():
    assert tracking_url("amazon", "TBA123456789000") == "https://www.swiship.com/track?id=TBA123456789000"


def test_unknown_carrier():
    assert tracking_url("nopecarrier", "ABC123") is None
    assert tracking_url("nopecarrier", "ABC123", merchant_link="https://shop.example/o/1") == "https://shop.example/o/1"


def test_order_level_uses_merchant_link():
    assert tracking_url("ups", None, merchant_link="https://shop.example/o/1") == "https://shop.example/o/1"
    assert tracking_url("ups", None) is None


@pytest.mark.parametrize(
    "bad",
    ["javascript:alert(1)", "data:text/html,x", "http://shop.example/o", "/relative", "https://", "https://a.b/ x"],
)
def test_non_https_merchant_link_rejected(bad):
    assert tracking_url("ups", None, merchant_link=bad) is None
    assert tracking_url("nopecarrier", "ABC", merchant_link=bad) is None
