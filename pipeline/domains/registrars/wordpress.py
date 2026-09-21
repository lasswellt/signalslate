"""
WordPress.com registrar adapter: best-effort, read-only inventory via the undocumented
GET /rest/v1.1/all-domains endpoint (docs/_research/2026-09-21_domain-collector.md Q3, Finding 2/3).

Design decisions:

- Read-only by construction: check() and purchase() raise Unsupported unconditionally, since
  WordPress.com has no documented availability-check or purchase API (Research Q3/Q5, Finding 3).
  list_domains() and check_connection() are the only operations this adapter performs.
- The endpoint was probed but is undocumented (module docstring cross-ref, Research Q3): its
  response shape can change without notice, so parsing is defensive end to end. Anything that is
  not the expected shape (not a dict, no "domains" list, an entry that is not a dict) is treated as
  "shape changed", never as a KeyError/TypeError escaping to the caller. Per-field values (expiry,
  auto_renew) are read with .get() and mapped to None when absent or the wrong type, mirroring
  godaddy.py's _parse_domain: a missing optional field is not itself a shape change, only a missing
  or malformed "domains" list is.
- Auth is a stored access_token (pipeline.oauth_wordpress / pipeline.connections, secret name
  "access_token"), sent as `Authorization: Bearer <token>`, never a client_id/client_secret pair:
  those two are only ever used by the OAuth exchange in oauth_wordpress.py, not by this adapter.
  No access_token on the connection (never signed in) is the same failure as the provider rejecting
  one (401/403 authorization_required): both raise AuthFailed("sign in again"), since either way
  the fix is the same browser sign-in flow.
- Exception messages are always fixed and generic, same reasoning as namecheap.py/godaddy.py: the
  response body could echo request data, and secrets must never reach an exception message.
"""
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import requests

from pipeline import connections
from pipeline.domains.registrars import (
    AuthFailed,
    Contact,
    PurchaseResult,
    Quote,
    RegistrarDomain,
    RegistrarError,
    Unsupported,
)

_log = logging.getLogger(__name__)

_TIMEOUT = 30
_ALL_DOMAINS_URL = "https://public-api.wordpress.com/rest/v1.1/all-domains"


@dataclass(frozen=True)
class _Config:
    access_token: str


def _load_config(connection_id: str) -> _Config:
    """
    Reads a wordpress connection's stored access_token. Raises AuthFailed for anything that makes
    the connection unusable to call the API with (no such connection, or never signed in), rather
    than a bare KeyError/ValueError: callers treat every Registrar failure as one of the typed
    errors in registrars/__init__.py.
    """
    view = connections.get(connection_id)
    if view is None:
        raise AuthFailed("no such connection")
    access_token = connections.get_secret(connection_id, "access_token")
    if not access_token:
        raise AuthFailed("sign in again")
    return _Config(access_token=access_token)


def _auth_headers(config: _Config) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.access_token}"}


def _parse_expires(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return None


def _parse_domain(entry: dict[str, Any]) -> Optional[RegistrarDomain]:
    """One entry of the "domains" list; None when the entry itself has no usable name."""
    name = entry.get("domain")
    if not isinstance(name, str) or not name:
        return None
    auto_renew = entry.get("auto_renewing")
    return RegistrarDomain(
        name=name.lower(),
        expires_at=_parse_expires(entry.get("expiry")),
        auto_renew=auto_renew if isinstance(auto_renew, bool) else None,
        # UNVERIFIED (module docstring): the undocumented response has no confirmed locked field.
        locked=None,
        # UNVERIFIED (module docstring): the undocumented response has no confirmed privacy field.
        privacy=None,
        nameservers=(),
    )


class WordPressClient:
    """Read-only registrar adapter for WordPress.com's undocumented all-domains inventory."""

    kind = "wordpress"

    def __init__(self, connection_id: str) -> None:
        self._connection_id = connection_id
        self._config = _load_config(connection_id)

    def _get(self, url: str) -> Any:
        try:
            response = requests.get(url, headers=_auth_headers(self._config), timeout=_TIMEOUT)
        except requests.RequestException:
            raise RegistrarError("WordPress.com API request failed") from None
        if response.status_code in (401, 403):
            raise AuthFailed("sign in again")
        if response.status_code >= 400:
            raise RegistrarError("WordPress.com API request failed")
        try:
            return response.json()
        except ValueError:
            raise RegistrarError("WordPress.com API returned an unparseable response") from None

    def list_domains(self) -> list[RegistrarDomain]:
        """
        Every domain WordPress.com's undocumented all-domains endpoint reports. Raises
        RegistrarError("response shape changed") when the response is not the documented-by-probe
        shape (a dict with a "domains" list), rather than letting a KeyError/TypeError escape
        (module docstring).
        """
        data = self._get(_ALL_DOMAINS_URL)
        if not isinstance(data, dict):
            raise RegistrarError("response shape changed")
        entries = data.get("domains")
        if not isinstance(entries, list):
            raise RegistrarError("response shape changed")
        domains: list[RegistrarDomain] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            parsed = _parse_domain(entry)
            if parsed is not None:
                domains.append(parsed)
        return domains

    def check(self, names: Sequence[str]) -> list[Quote]:
        """WordPress.com has no documented availability-check API (module docstring)."""
        raise Unsupported("WordPress.com does not support availability checks")

    def purchase(self, quote: Quote, years: int, contact: Contact) -> PurchaseResult:
        """WordPress.com has no documented purchase API (module docstring)."""
        raise Unsupported("WordPress.com does not support purchasing")

    def check_connection(self) -> str:
        """Confirms the stored access_token works by calling the same inventory endpoint."""
        self.list_domains()
        return "connected to WordPress.com"
