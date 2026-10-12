"""
Carrier deep-link builder for the /packages page.

Design decisions:

- Templates come from the vendored jkeen courier JSON (pipeline/packages/data/couriers/*.json),
  the same source numbers.py classifies with, so a carrier recognised there links there. A
  per-format template (matched by format id such as "fedex_12") wins over the courier-level one
  (first format of the courier that carries a tracking_url; jkeen has no courier-level key).
- Every returned URL is https. A jkeen http:// template (DHL) is upgraded to https rather than
  emitted, so the page never links to cleartext.
- The tracking number is percent-encoded with no safe characters, so odd input (spaces, '&',
  '#', '/') cannot inject query parameters or alter the URL path.
- Carriers jkeen lacks (walmart, sefl, amazon TBA) are small explicit overrides.
- merchant_link originates from untrusted email text: only https links with a host are accepted,
  which rejects javascript:, data:, http: and relative links. It is the fallback for unknown
  carriers and for order-level packages that have no tracking number.
"""
import json
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

_log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data" / "couriers"
_NUMBER_TOKEN = "%s"

# Public SEFL (Southeastern Freight Lines) tracing page; not in jkeen.
_SEFL_URL = "https://www.sefl.com/Tracing/index.jsp?Type=PN&RefNum1=%s"
_WALMART_ORDERS_URL = "https://www.walmart.com/orders"
# Amazon Logistics "TBA" numbers are trackable on the public Swiship page.
_AMAZON_TBA_URL = "https://www.swiship.com/track?id=%s"

_templates: Optional[tuple[dict[str, str], dict[str, str]]] = None


def _load_templates() -> tuple[dict[str, str], dict[str, str]]:
    """
    Loads (courier_code -> template, format_id -> template) from the vendored courier JSON once.

    Returns:
        Two maps; templates that are not https/http or lack '%s' are skipped.
    """
    global _templates
    if _templates is not None:
        return _templates
    by_courier: dict[str, str] = {}
    by_format: dict[str, str] = {}
    for path in sorted(_DATA_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _log.warning("courier file %s unreadable: %s", path.name, type(exc).__name__)
            continue
        code = str(data.get("courier_code") or path.stem).lower()
        for fmt in data.get("tracking_numbers", []):
            tpl = fmt.get("tracking_url")
            if not isinstance(tpl, str) or _NUMBER_TOKEN not in tpl:
                continue
            if tpl.startswith("http://"):
                tpl = "https://" + tpl[len("http://"):]
            if not tpl.startswith("https://"):
                continue
            if fmt.get("id"):
                by_format[str(fmt["id"]).lower()] = tpl
            by_courier.setdefault(code, tpl)
    _templates = (by_courier, by_format)
    return _templates


def safe_https_link(url: Optional[str]) -> Optional[str]:
    """
    Returns url only if it is an absolute https link with a host.

    Args:
        url: Untrusted link text (may be None).

    Returns:
        The stripped url, or None for javascript:/data:/http:/relative/malformed input.
    """
    if not url:
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname:
        return None
    if any(c.isspace() or ord(c) < 32 for c in url):
        return None
    return url


def tracking_url(
    carrier: str,
    tracking_number: Optional[str],
    *,
    merchant_domain: Optional[str] = None,
    order_id: Optional[str] = None,
    merchant_link: Optional[str] = None,
) -> Optional[str]:
    """
    Builds the deep link for a package.

    Args:
        carrier: Carrier key (jkeen courier_code or format id, e.g. "ups", "fedex_12", "walmart").
        tracking_number: Carrier tracking number, or None for an order-level package.
        merchant_domain: Merchant domain (accepted for API symmetry; links never derive from it).
        order_id: Merchant order id; used by the walmart order page.
        merchant_link: Untrusted merchant link captured from the email; used as fallback.

    Returns:
        An https URL, or None when no safe link can be built.
    """
    key = (carrier or "").strip().lower()
    number = (tracking_number or "").strip()
    fallback = safe_https_link(merchant_link)

    if key == "walmart":
        if order_id and order_id.strip():
            return f"{_WALMART_ORDERS_URL}/{quote(order_id.strip(), safe='')}"
        return _WALMART_ORDERS_URL
    if not number:
        return fallback

    encoded = quote(number, safe="")
    if key == "sefl":
        return _SEFL_URL.replace(_NUMBER_TOKEN, encoded)
    if key == "amazon" and number.upper().startswith("TBA"):
        return _AMAZON_TBA_URL.replace(_NUMBER_TOKEN, encoded)

    by_courier, by_format = _load_templates()
    tpl = by_format.get(key) or by_courier.get(key)
    if tpl:
        return tpl.replace(_NUMBER_TOKEN, encoded)
    return fallback
