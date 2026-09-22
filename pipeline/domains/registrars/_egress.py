"""
The best-effort "what is this server's public IPv4" lookup, shared by every caller that needs to
report or pre-fill an egress IP for a Namecheap/GoDaddy-style IP allowlist: godaddy.py's purchase()
consent.agreedBy, namecheap.py's check_connection() "whitelist X" message, and
api/routers/domains.py's GET /domains/egress-ip (called before any connection exists, so it cannot
go through a registrar client). godaddy.py and namecheap.py each carried an identical copy of this
before the third caller made deduplicating it worth doing.
"""
import requests

_IPIFY_URL = "https://api.ipify.org"
_TIMEOUT = 30


def current_egress_ip() -> str:
    """Never raises; "unknown" on any failure, matching how every caller already displays it."""
    try:
        response = requests.get(_IPIFY_URL, params={"format": "text"}, timeout=_TIMEOUT)
        response.raise_for_status()
        return response.text.strip()
    except requests.RequestException:
        return "unknown"
