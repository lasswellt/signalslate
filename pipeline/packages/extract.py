"""
Tiered shipment extraction from one mail (sender, subject, html/plain body, receive time).

Design decisions:

- Tiers run strongest first and a number found by an earlier tier is never re-reported by a later
  one (dedupe by number, so the first tier's carrier and confidence win): (1) sender rules
  (Walmart 555 last-mile numbers, SEFL Pro numbers, FedEx Delivery Manager, DHL On Demand),
  (2) labeled values ("Tracking #", "<Carrier> tracking number:", "Waybill", "Parcel number"),
  (3) every jkeen pattern over the visible text, (4) tracking numbers found in link targets.
- Walmart 555 numbers are claimed by the sender rule before any checksum classifier runs, because
  one in ten of them passes the USPS 20 check by chance and would then be polled at USPS forever.
- A value that follows an order/item/phone/reference label is never a tracking number (a 14-digit
  order id matches the checksum-less DHL E-Commerce pattern). It may become the order id instead.
- A labeled value that matches no known format is kept with confidence "labeled" and the carrier
  from an adjacent carrier word (Home Depot 1R00... -> ups); checksum-validated carriers win over
  adjacent words, adjacent words win over weak or checksum-less pattern matches.
- Visible text is URL-stripped before scanning so tracking-looking digits inside link paths are
  only considered by the URL tier, which knows which parameter they came from.
- A mail that names an order but carries no number yields ONE order-level Shipment (tracking
  number None); with no order id it yields nothing.
- Pure function, no I/O. Mail bodies are never logged or persisted; only extracted fields leave.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from typing import Optional

from pipeline.packages import html as pkg_html
from pipeline.packages import numbers

_URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+")
_LABEL = re.compile(
    r"(tracking\s*(number|no\.?|#|id)?|track(ing)?\s*#|waybill( number)?|parcel number|"
    r"track your package)[\s:#|]*(with\s+)?$", re.I)
_ORDER_LABEL = re.compile(
    r"(order\s*(number|no\.?|#|id)?|shipment\s*#|customer\s*no\.?|reference(\s*id)?|invoice|"
    r"item(\s*id)?|sku|internet\s*#|confirmation\s*#?|phone(\s*number)?|call|text|reach me at|"
    r"member|account)[\s:#|\[\]()+]*$", re.I)
_LABELED_VALUE = re.compile(
    r"(?i:tracking\s*(?:numbers?(?:\(s\))?|no\.?|#|id)|track(?:ing)?\s*#|waybill(?:\s*number)?|"
    r"parcel number|track your package)(?:\s*(?i:for these items is))?[\s:#|]*"
    r"((?:[A-Z0-9]|[ -](?=\d)){8,40}?)(?=[^A-Z0-9]|[A-Z][a-z]|$)")
_PRO = re.compile(r"\bpro\s*(?:number|no\.?|#)\s*:?\s*(\d{6,12})\b|\bpronumber\s*:?\s*(\d{6,12})\b",
                  re.I)
_WALMART_LABELED = re.compile(r"track your package\s*:\s*(555\d{17})(?!\d)", re.I)
_WALMART_ANY = re.compile(r"(?<!\d)(555\d{17})(?!\d)")
_SHOPIFY = re.compile(
    r"a shipment from order\s+#?(\S+?)\s+(?:is on the way|is out for delivery|has been delivered)",
    re.I)
_ORDER_ID = re.compile(
    r"\border\s*(?:(?:number|no\b\.?|id\b)|#)\s*[:#]?\s*(?P<l>[A-Za-z0-9][A-Za-z0-9-]{3,29})"
    r"|\border\s+(?P<b>(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{3,29})", re.I)
_CARRIER_WORD = re.compile(r"\b(ups|usps|fedex|dhl|yunexpress|amazon|ontrac|lasership)\b", re.I)
_CARRIER_CUE = re.compile(
    r"(?:carrier|shipped\s+(?:with|via|by)|delivery\s+by|shipping\s+service)\s*:?\s*"
    r"(ups|usps|fedex|dhl|yunexpress|amazon|ontrac|lasership)\b", re.I)
_FEDEX_MERCHANT = re.compile(r"your shipment from (.+?)\s+(?:is|has|was|will)\b", re.I)
_FEDEX_REFERENCE = re.compile(r"\breference\s*:?\s*([A-Za-z0-9-]{4,30})", re.I)
_FEDEX_ID = re.compile(r"tracking id\s*:?\s*(\d{12,22})\b", re.I)
_FEDEX_SUBJECT_TAIL = re.compile(r"(\d{12,22})\s*$")
_DHL_WAYBILL = re.compile(r"waybill number\s*:?\s*(\d{8,12})\b", re.I)
_DHL_MERCHANT = re.compile(r"\bfrom (.+?)\s+(?:has|is|was|will)\b")


@dataclass(frozen=True)
class Shipment:
    """
    One package (or order-level placeholder) found in a mail.

    Attributes:
        carrier: Lowercase courier code, "unknown" when none could be determined.
        tracking_number: Tracking number, None for an order-level shipment.
        order_id: Merchant order id captured from the mail, if any.
        merchant: Display name (or domain) of the sender, or the "from <X>" text for FedEx/DHL.
        merchant_domain: Last two labels of the sender address domain.
        source: sender-rule | label | pattern | url | order-only.
        confidence: high | labeled | pattern.
    """

    carrier: str
    tracking_number: Optional[str]
    order_id: Optional[str]
    merchant: Optional[str]
    merchant_domain: Optional[str]
    source: str
    confidence: str


@dataclass(frozen=True)
class MailInput:
    """
    The parts of a mail extraction needs.

    Attributes:
        sender: Raw From header value.
        subject: Subject line.
        html: HTML body or None.
        plain: Plain-text body or None.
        received_at: Receive time (naive UTC).
    """

    sender: str
    subject: str
    html: Optional[str]
    plain: Optional[str]
    received_at: datetime


def _sender_parts(sender: str) -> tuple[Optional[str], str, Optional[str]]:
    """Returns (display name, lowercase address domain, registrable domain) of a From value."""
    name, addr = parseaddr(sender)
    domain = addr.rsplit("@", 1)[1].lower() if "@" in addr else ""
    reg = ".".join(domain.split(".")[-2:]) if domain else None
    return (name.strip() or None), domain, reg


def _valid_order_token(tok: str, labeled: bool) -> bool:
    if labeled:
        return any(c.isdigit() for c in tok) or not any(c.islower() for c in tok)
    return True


def _order_id(*texts: str) -> Optional[str]:
    """Returns the first order id in the texts (labeled or "order <id-with-digit>"), or None."""
    for text in texts:
        for m in _ORDER_ID.finditer(text):
            tok = (m.group("l") or m.group("b")).rstrip("-")
            if len(tok) >= 4 and _valid_order_token(tok, m.group("l") is not None):
                return tok
    return None


def _word_before(text: str, pos: int) -> Optional[str]:
    """Last carrier word in the 120 characters before pos, as a lowercase code."""
    found = _CARRIER_WORD.findall(text[max(0, pos - 120):pos])
    return found[-1].lower() if found else None


def _value_ok(tok: str) -> bool:
    return any(c.isdigit() for c in tok) and not (tok.isdigit() and len(tok) < 9)


def extract(mail: MailInput) -> list[Shipment]:
    """
    Extracts every distinct package in a mail, strongest evidence first.

    Args:
        mail: The mail's sender, subject, bodies and receive time.

    Returns:
        One Shipment per distinct tracking number (first tier to find it wins). When no number
        was found but an order id was captured, a single order-level Shipment (tracking_number
        None); otherwise an empty list.
    """
    plain = mail.plain or ""
    h = pkg_html.harvest(mail.html, plain)
    text = _URL_RE.sub(" ", h.text)
    full = mail.subject + "\n" + text
    name, domain, reg = _sender_parts(mail.sender)
    merchant = name or reg
    sender_l = mail.sender.lower()
    found: dict[str, Shipment] = {}
    fedex_ref: Optional[str] = None

    def add(carrier: str, number: str, source: str, confidence: str,
            merch: Optional[str] = None) -> None:
        if number in found or any(number in k for k in found if len(k) > len(number)):
            return
        found[number] = Shipment(carrier, number, None, merch or merchant, reg, source, confidence)

    # Tier 1: sender rules
    if domain.endswith("walmart.com"):
        for rx in (_WALMART_LABELED, _WALMART_ANY):
            for m in rx.finditer(full):
                add("walmart", m.group(1), "sender-rule", "high")
    for m in _PRO.finditer(full):
        add("sefl", m.group(1) or m.group(2), "sender-rule", "high")
    if domain.endswith("fedex.com"):
        fm = _FEDEX_MERCHANT.search(full)
        fed_merchant = fm.group(1).strip() if fm else None
        rm = _FEDEX_REFERENCE.search(full)
        fedex_ref = rm.group(1) if rm else None
        nums = [m.group(1) for m in (_FEDEX_SUBJECT_TAIL.search(mail.subject), _FEDEX_ID.search(full))
                if m]
        for n in nums:
            add("fedex", n, "sender-rule", "high", fed_merchant)
    if domain.endswith("dhl.com"):
        dm = _DHL_MERCHANT.search(full)
        for m in _DHL_WAYBILL.finditer(full):
            add("dhl", m.group(1), "sender-rule", "high", dm.group(1).strip() if dm else None)

    # Tier 2: labeled values
    for m in _LABELED_VALUE.finditer(full):
        tok = re.sub(r"[\s-]", "", m.group(1))
        if not _value_ok(tok):
            continue
        ctx = full[max(0, m.start(1) - 80):m.start(1)] + " " + sender_l + " " + mail.subject
        best = numbers.best_match(tok, context=ctx, labeled=True)
        word = _word_before(full, m.start())
        strong = best is not None and best.checksum is True and not best.weak
        carrier = best.carrier if strong else word or (best.carrier if best else "unknown")
        add(carrier, tok, "label", "high" if strong else "labeled")

    # Tier 3: any known pattern in the visible text
    for tok, start, _ in numbers.find_candidates(full):
        pre = full[max(0, start - 40):start]
        if _ORDER_LABEL.search(pre):
            continue
        ctx = full[max(0, start - 80):start] + " " + sender_l + " " + mail.subject
        best = numbers.best_match(tok, context=ctx, labeled=bool(_LABEL.search(pre)))
        if best:
            strong = best.checksum is True and not best.weak
            add(best.carrier, tok, "pattern", "high" if strong else "pattern")

    # Tier 4: tracking numbers in link targets
    for uc in h.url_candidates:
        best = numbers.best_match(
            uc.token, context=uc.source_url_host + " " + (uc.carrier_hint or "") + " " + sender_l,
            labeled=True)
        if best:
            strong = best.checksum is True and not best.weak
            add(best.carrier if strong else uc.carrier_hint or best.carrier, uc.token, "url",
                "high" if strong else "pattern")

    shopify = _SHOPIFY.search(mail.subject)
    order = (shopify.group(1) if shopify else None) or fedex_ref or _order_id(mail.subject, full)
    if found:
        return [Shipment(s.carrier, s.tracking_number, order, s.merchant, s.merchant_domain,
                         s.source, s.confidence) for s in found.values()]
    if not order:
        return []
    cue = _CARRIER_CUE.search(full)
    carrier = cue.group(1).lower() if cue else "walmart" if domain.endswith("walmart.com") else "unknown"
    return [Shipment(carrier, None, order, merchant, reg, "order-only", "pattern")]
