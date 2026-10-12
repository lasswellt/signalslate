"""
Shipping-status classification from a single notification email.

Design decisions:

- Pure function over (sender, subject, text, received_at): no network, no DB, no logging of mail
  content. Callers decide what to persist; this module only returns a small StatusSignal.
- Privacy: the description is a fixed label for the matched phrase (or the subject, capped at 120
  chars). Body text is only ever scanned, never copied into the result.
- Rules run in order: global noise rejection (substitution, review, refund, survey, promo, duty and
  payment mail -> None), then per-sender family rules built from observed merchant templates, then
  a generic subject-first fallback that requires a transactional-looking subject so marketing
  copy such as "promised but never delivered" cannot mark a parcel delivered.
- Status values come from pipeline.packages.STATUSES. Exception covers delays, failed delivery and
  cancellations; the description distinguishes "canceled".
- ETA is parsed from labelled phrases only (arrives, arrive by, estimated/expected/scheduled
  delivery). Dates without a year take the mail's year, rolling to the next year when the result
  would be more than 30 days before receipt. Delivered mails carry no ETA.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

_MAX_DESC = 120
_TEXT_WINDOW = 400

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass(frozen=True)
class StatusSignal:
    """A shipping status inferred from one email.

    Attributes:
        status: One of pipeline.packages.STATUSES.
        occurred_at: The mail's received_at.
        eta: Estimated delivery date when the mail states one.
        description: Short label for the matched phrase, at most 120 characters.
    """
    status: str
    occurred_at: datetime
    eta: Optional[date]
    description: str


_NOISE_SUBJECT = re.compile(
    r"substitut|take action|reminder|review your|leave a review|write a review|refund|survey|"
    r"how was|rate your|feedback|restocked|back in stock|import dut|customs|duties|"
    r"payment (?:due|required|needed|received)|pay now|\d+% off|\bsale\b|\bdeals?\b|"
    r"coupon|promo|newsletter|unsubscribe", re.I)
_NOISE_SENDER = re.compile(r"nextdoor", re.I)
_WALMART_TEXT_NOISE = re.compile(r"substitution", re.I)

_TRANSACTIONAL = re.compile(r"order|package|parcel|shipment|delivery|#", re.I)
_NEGATED_DELIVERED = re.compile(r"never (?:been )?(?:delivered|arrived)|not (?:been )?delivered", re.I)

_DATE_LABEL = (r"(?:arrives?|arrive by|arriving|estimated delivery(?: date)?|expected delivery|"
               r"scheduled delivery date|delivery date|estimated arrival)")
_MONTH_DAY = re.compile(
    _DATE_LABEL + r"\s*:?\s*(?:[A-Za-z]{3,9},?\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?\b", re.I)
_NUMERIC = re.compile(
    _DATE_LABEL + r"\s*:?\s*(?:[A-Za-z]{3,9},?\s+)?(\d{1,2})/(\d{1,2})/(\d{4})\b", re.I)


def _resolve_year(month: int, day: int, year: Optional[int], received: datetime) -> Optional[date]:
    """Build a date, inferring the year from the receipt date when absent."""
    try:
        if year is not None:
            return date(year, month, day)
        candidate = date(received.year, month, day)
        if candidate < received.date() - timedelta(days=30):
            candidate = date(received.year + 1, month, day)
        return candidate
    except ValueError:
        return None


def _parse_eta(blob: str, received: datetime) -> Optional[date]:
    """Find a labelled delivery date in text.

    Args:
        blob: Subject plus leading body text to scan.
        received: Mail receipt time used for year inference.

    Returns:
        The date, or None when no labelled date parses.
    """
    m = _NUMERIC.search(blob)
    if m:
        found = _resolve_year(int(m.group(1)), int(m.group(2)), int(m.group(3)), received)
        if found:
            return found
    for m in _MONTH_DAY.finditer(blob):
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month:
            found = _resolve_year(month, int(m.group(2)), int(m.group(3)) if m.group(3) else None, received)
            if found:
                return found
    return None


def _first(rules: list[tuple[re.Pattern[str], str, str]], blob: str) -> Optional[tuple[str, str]]:
    """Return (status, description) of the first rule matching blob."""
    for pattern, status, desc in rules:
        if pattern.search(blob):
            return status, desc
    return None


def _rule(pattern: str, status: str, desc: str) -> tuple[re.Pattern[str], str, str]:
    return re.compile(pattern, re.I), status, desc


_WALMART = [
    _rule(r"canceled:\s*delivery of|delivery .{0,20}canceled", "exception", "Walmart: canceled"),
    _rule(r"your (?:order|delivery) is delayed", "exception", "Walmart: delayed"),
    _rule(r"^out for delivery:|should arrive by \d{1,2}:\d{2}\s*[ap]m|your (?:package|delivery) should arrive by",
          "out_for_delivery", "Walmart: out for delivery"),
    _rule(r"^(?:arrived|delivered):", "delivered", "Walmart: delivered"),
    _rule(r"^shipped:|your package is in transit", "in_transit", "Walmart: shipped"),
    _rule(r"thanks for your (?:inhome )?(?:in-home )?delivery order", "pending", "Walmart: order placed"),
]
_FEDEX = [
    _rule(r"out for delivery today", "out_for_delivery", "FedEx: out for delivery"),
    _rule(r"was delivered|has been delivered", "delivered", "FedEx: delivered"),
    _rule(r"scheduled for delivery tomorrow", "in_transit", "FedEx: scheduled for delivery tomorrow"),
    _rule(r"is on the way", "in_transit", "FedEx: on the way"),
]
_DHL = [
    _rule(r"delivery is today|arriving soon", "out_for_delivery", "DHL: out for delivery"),
    _rule(r"\bdelivered\b", "delivered", "DHL: delivered"),
    _rule(r"on its way", "in_transit", "DHL: on its way"),
]
_HOMEDEPOT = [
    _rule(r"part of your order has been delivered", "delivered", "Home Depot: partially delivered"),
    _rule(r"out for delivery today", "out_for_delivery", "Home Depot: out for delivery"),
    _rule(r"we've delivered your order|we.ve delivered your order|\bdelivered\b", "delivered", "Home Depot: delivered"),
    _rule(r"\bshipped\b", "in_transit", "Home Depot: shipped"),
    _rule(r"order confirmed|we received your order", "pending", "Home Depot: order confirmed"),
]
_SEFL = [
    _rule(r"status:\s*out for delivery", "out_for_delivery", "Freight: out for delivery"),
    _rule(r"status:\s*delivered", "delivered", "Freight: delivered"),
    _rule(r"status:\s*dispatched", "in_transit", "Freight: dispatched"),
]
_SHOPIFY = [
    _rule(r"a shipment from order .{1,40}?(?:is out for delivery)", "out_for_delivery", "Shipment out for delivery"),
    _rule(r"a shipment from order .{1,40}?(?:has been delivered)", "delivered", "Shipment delivered"),
    _rule(r"a shipment from order .{1,40}?(?:is on the way)", "in_transit", "Shipment on the way"),
]
_EBAY = [
    _rule(r"out for delivery", "out_for_delivery", "eBay: out for delivery"),
    _rule(r"order delivered", "delivered", "eBay: delivered"),
    _rule(r"now with its carrier", "in_transit", "eBay: with carrier"),
]
_GENERIC = [
    _rule(r"return to sender|\breturned\b", "returned", "returned"),
    _rule(r"delivery exception|\bdelayed\b|failed delivery|delivery attempt failed", "exception", "delivery problem"),
    _rule(r"out for delivery", "out_for_delivery", "out for delivery"),
    _rule(r"has been delivered|\bdelivered\b|\barrived\b", "delivered", "delivered"),
    _rule(r"\bshipped\b|on its way|on the way|dispatched|has been sent|in transit", "in_transit", "shipped"),
    _rule(r"order confirmed|we received your order", "pending", "order confirmed"),
]
_GENERIC_TEXT_TRACKING = re.compile(
    r"order success.{0,80}tracking information|tracking information.{0,120}track shipment", re.I | re.S)


def _signal(status: str, desc: str, eta_blob: str, received: datetime) -> StatusSignal:
    """Assemble a StatusSignal, parsing an ETA unless the parcel is already delivered."""
    eta = None if status in ("delivered", "returned") else _parse_eta(eta_blob, received)
    return StatusSignal(status=status, occurred_at=received, eta=eta, description=desc[:_MAX_DESC])


def _family_rules(sender: str, subject: str) -> Optional[list[tuple[re.Pattern[str], str, str]]]:
    """Pick the per-sender rule set, or None for the generic path."""
    if sender.endswith("walmart.com"):
        return _WALMART
    if sender.endswith("fedex.com"):
        return _FEDEX
    if sender.endswith("dhl.com"):
        return _DHL
    if sender.endswith("homedepot.com"):
        return _HOMEDEPOT
    if sender.endswith("ebay.com"):
        return _EBAY
    if re.search(r"pro#\s*\d+\s*status:", subject, re.I):
        return _SEFL
    if re.match(r"a shipment from order\b", subject.strip(), re.I):
        return _SHOPIFY
    return None


def classify_status(sender: str, subject: str, text: str, received_at: datetime) -> Optional[StatusSignal]:
    """Infer the shipping status a notification email reports.

    Args:
        sender: From address (any case; display name tolerated).
        subject: Message subject.
        text: Plain-text body; only the leading window and labelled dates are inspected.
        received_at: When the mail arrived; becomes occurred_at and anchors year inference.

    Returns:
        A StatusSignal, or None when the mail is not a shipping status update (substitution,
        review, reminder, refund, survey, promotion, payment or import-duty mail).
    """
    sender_l = (sender or "").strip().lower()
    if sender_l.endswith(">"):
        sender_l = sender_l.rsplit("<", 1)[-1].rstrip(">")
    subject = (subject or "").strip()
    text = text or ""
    if _NOISE_SENDER.search(sender_l) or _NOISE_SUBJECT.search(subject):
        return None
    if sender_l.endswith("walmart.com") and _WALMART_TEXT_NOISE.search(text[:_TEXT_WINDOW]):
        return None
    head = text[:_TEXT_WINDOW]
    eta_blob = subject + " \n" + text[:2000]

    rules = _family_rules(sender_l, subject)
    if rules is not None:
        hit = _first(rules, subject)
        if hit is None and rules is not _WALMART and rules is not _SHOPIFY:
            hit = _first(rules, head)
        if hit is not None:
            return _signal(hit[0], hit[1], eta_blob, received_at)
        if rules is not _HOMEDEPOT and sender_l.endswith(("walmart.com", "fedex.com", "ebay.com")):
            return None

    if _NEGATED_DELIVERED.search(subject) or not _TRANSACTIONAL.search(subject):
        return None
    hit = _first(_GENERIC, subject)
    if hit is None and not _NEGATED_DELIVERED.search(head):
        hit = _first(_GENERIC, head)
    if hit is None and _GENERIC_TEXT_TRACKING.search(head):
        hit = ("in_transit", "tracking update")
    if hit is None:
        return None
    desc = subject if subject else hit[1]
    if len(desc) > _MAX_DESC:
        desc = desc[:_MAX_DESC - 3] + "..."
    return _signal(hit[0], desc, eta_blob, received_at)
