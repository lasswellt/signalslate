"""
Provider-agnostic pieces of the browser sign-in: the pending-flow store, the nonce that binds a flow to
the browser that started it, the paste-back URL parser and the callback URL builder
(docs/_research/2026-09-21_management-ui.md sections 5 and 6, RFC 9700).

Pure logic on purpose: nothing here does I/O, reads the environment or knows Google or Microsoft
specifics, so oauth_gmail and oauth_m365 (and the API routes) can share it.

Design decisions:

* The store is in-process. The app runs as one uvicorn process, so a dict behind a lock is enough, and
  pending flows do NOT survive a restart: a half-finished sign-in must be started again. That is
  acceptable (a flow lives at most 15 minutes) and it keeps the PKCE verifier off disk.
* A flow's `payload` (the provider's flow dict, including the code_verifier) never leaves the server.
  PendingFlow hides it, the state and the nonce hash from repr so an assertion failure or a stray log
  line cannot print them.
* The nonce is 256 random bits, handed to the caller once (it becomes an HttpOnly cookie) and stored
  only as a SHA-256 digest. The digest is compared with hmac.compare_digest.
* A WRONG nonce (or a wrong provider) does not consume the record. State travels through the browser's
  address bar and is visible to anything that can see a URL, so if a bad nonce burned the flow, whoever
  learned a victim's state could cancel the victim's sign-in at will. The record is only consumed by
  the holder of the nonce, or by expiry.
* A missing nonce is a mismatch: there is no unbound mode, so a caller that forgets the cookie fails
  closed.
* Exceptions carry a fixed message and no arguments. An OAuth code, a state or a pasted URL never
  reaches an exception message, a log line or a traceback.
"""
import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from pipeline.clock import utcnow

MODES = ("paste_back", "callback")
PROVIDERS = ("google", "microsoft")

DEFAULT_TTL_SECONDS = 900
MAX_PENDING = 20

# The URL a user pastes is a Google/Microsoft redirect to loopback: a few hundred bytes. 4 KB leaves
# room for long error descriptions while bounding what an attacker can make us parse.
MAX_URL_LENGTH = 4096
MAX_FIELD_LENGTH = 2048
_MAX_QUERY_FIELDS = 50
_PASTED_KEYS = ("code", "state", "error", "error_description")
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost")


class FlowError(Exception):
    """
    Base for every expected sign-in failure.

    The message is a class constant and the constructor takes no arguments: there is no way to pass a
    code or a URL in, which is what makes "no exception message contains a secret" hold by construction.
    """

    message = "sign-in flow error"

    def __init__(self) -> None:
        super().__init__(self.message)


class UnknownFlow(FlowError):
    """No pending flow matches: never started, already used, or evicted. Also what a forged callback gets."""

    message = "unknown or already used sign-in flow"


class ExpiredFlow(FlowError):
    """The flow's TTL elapsed; the record is gone and the sign-in must be started again."""

    message = "sign-in flow expired"


class NonceMismatch(FlowError):
    """The browser presenting the flow is not the one that started it (or sent no nonce)."""

    message = "sign-in flow does not belong to this browser"


class ProviderMismatch(FlowError):
    """The flow belongs to another provider than the callback path it arrived on (mix-up defense)."""

    message = "sign-in flow belongs to a different provider"


class InvalidPastedUrl(FlowError):
    """The pasted text is not an acceptable loopback redirect URL."""

    message = "pasted URL is not a valid loopback redirect"


class FragmentOnlyResponse(InvalidPastedUrl):
    """The provider answered in the URL fragment, which a pasted URL can carry but a server never sees."""

    message = "the response is in the URL fragment (after #); restart sign-in in query mode (response_mode=query)"


class InvalidCallbackConfig(FlowError):
    """The public base URL or provider cannot form a callback URL."""

    message = "callback URL cannot be built from the configured base URL or provider"


@dataclass(frozen=True)
class PendingFlow:
    """One in-flight sign-in. `payload` holds the provider flow dict (code_verifier included): server-side only."""

    flow_id: str
    provider: str
    connection_id: str
    mode: str
    state: str = field(repr=False)
    redirect_uri: str
    payload: dict[str, Any] = field(repr=False)
    nonce_hash: str = field(repr=False)
    created_at: datetime
    expires_at: datetime


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same(a: str, b: str) -> bool:
    # Encode first: compare_digest raises TypeError on non-ASCII str, and a hostile state may be anything.
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class FlowStore:
    """
    Thread-safe pending-flow store: single-use records with a TTL, a size cap and browser binding.

    Requests run on a threadpool, so every public method takes the lock. Time is injectable (`now`)
    because the TTL rules must be testable without sleeping.
    """

    def __init__(self, max_pending: int = MAX_PENDING) -> None:
        self._max_pending = max_pending
        self._lock = threading.Lock()
        # Insertion order is age order, which is what "oldest evicted" needs.
        self._flows: dict[str, PendingFlow] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._flows)

    def create(
        self,
        provider: str,
        connection_id: str,
        mode: str,
        state: str,
        redirect_uri: str,
        payload: dict[str, Any],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now: Optional[datetime] = None,
    ) -> tuple[PendingFlow, str]:
        """
        Register a new pending flow.

        :param state: the OAuth state the provider will echo back; must be non-empty.
        :param payload: provider flow dict (may hold code_verifier); stays server-side.
        :param now: injected clock for tests; defaults to utcnow().
        :returns: (flow, nonce). The nonce is the only copy: set it as the browser's cookie and forget it.
        :raises ValueError: for a programming error (unknown provider or mode, empty state, ttl <= 0).
        """
        if provider not in PROVIDERS or mode not in MODES or not state or ttl_seconds <= 0:
            raise ValueError("invalid pending-flow arguments")
        moment = now if now is not None else utcnow()
        nonce = secrets.token_urlsafe(32)
        flow = PendingFlow(
            flow_id=secrets.token_urlsafe(16),
            provider=provider,
            connection_id=connection_id,
            mode=mode,
            state=state,
            redirect_uri=redirect_uri,
            payload=payload,
            nonce_hash=_digest(nonce),
            created_at=moment,
            expires_at=moment + timedelta(seconds=ttl_seconds),
        )
        with self._lock:
            self._purge(moment)
            self._flows[flow.flow_id] = flow
            while len(self._flows) > self._max_pending:
                del self._flows[next(iter(self._flows))]
        return flow, nonce

    def consume(
        self,
        state: Optional[str] = None,
        flow_id: Optional[str] = None,
        nonce: Optional[str] = None,
        provider: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> PendingFlow:
        """
        Atomically look up a flow by `flow_id` or `state` and remove it.

        The callback route knows only the state, the paste route knows the flow_id. A wrong nonce or
        provider leaves the record in place (see the module docstring), so a third party cannot burn
        someone else's flow; only an expired record is removed on failure.

        :param provider: when given, the flow must belong to it (mix-up defense on the callback path).
        :raises UnknownFlow: no selector, no match, or already consumed.
        :raises ExpiredFlow: the match is past its TTL (it is removed).
        :raises NonceMismatch: nonce missing or not the one issued at create().
        :raises ProviderMismatch: `provider` differs from the flow's.
        """
        moment = now if now is not None else utcnow()
        with self._lock:
            flow = self._find(state, flow_id)
            self._purge(moment, keep=flow.flow_id if flow else None)
            if flow is None:
                raise UnknownFlow()
            if flow.expires_at <= moment:
                del self._flows[flow.flow_id]
                raise ExpiredFlow()
            if not isinstance(nonce, str) or not _same(_digest(nonce), flow.nonce_hash):
                raise NonceMismatch()
            if provider is not None and provider != flow.provider:
                raise ProviderMismatch()
            del self._flows[flow.flow_id]
            return flow

    def _find(self, state: Optional[str], flow_id: Optional[str]) -> Optional[PendingFlow]:
        # Selector strings are attacker-supplied: compare in constant time and scan every record rather
        # than return at the first hit (at most MAX_PENDING of them).
        if isinstance(flow_id, str) and flow_id:
            found = None
            for flow in self._flows.values():
                if _same(flow.flow_id, flow_id):
                    found = flow
            return found
        if isinstance(state, str) and state:
            found = None
            for flow in self._flows.values():
                if _same(flow.state, state):
                    found = flow
            return found
        return None

    def _purge(self, moment: datetime, keep: Optional[str] = None) -> None:
        # `keep` is the record consume() is about to inspect: purging it first would turn "expired"
        # into "unknown" and the user would lose the reason.
        for flow_id in [f.flow_id for f in self._flows.values() if f.expires_at <= moment and f.flow_id != keep]:
            del self._flows[flow_id]


def parse_pasted_url(url: str, *, allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS) -> dict[str, Optional[str]]:
    """
    Extract the OAuth response from a pasted loopback redirect URL, without ever fetching it.

    The user copies the address bar after the provider redirects to a loopback address nothing listens
    on. The text is untrusted input, so it is parsed only: the host must be exactly one of
    `allowed_hosts`, which stops a look-alike or an `http://127.0.0.1@evil.example/` from being treated
    as loopback, and the caller exchanges the code against the STORED redirect_uri, not this one.

    :returns: {code, state, error, error_description}; absent fields are None, present ones are capped
        at 2 KB.
    :raises FragmentOnlyResponse: the code arrived after `#`.
    :raises InvalidPastedUrl: anything else wrong (oversize, scheme, host, userinfo, no query, no state,
        neither code nor error, a repeated parameter). The message never includes the input.
    """
    if not isinstance(url, str):
        raise InvalidPastedUrl()
    text = url.strip()
    if not text or len(text) > MAX_URL_LENGTH:
        raise InvalidPastedUrl()
    # Browsers read a backslash as a slash, urllib does not: `http://evil.example\@127.0.0.1/` would
    # otherwise parse differently here than where the user pasted it from. Control characters and inner
    # whitespace are never part of a copied redirect.
    if "\\" in text or any(ch.isspace() or not ch.isprintable() for ch in text):
        raise InvalidPastedUrl()
    try:
        parts = urlsplit(text)
        # Reading .port validates it (digits, range); any loopback port is acceptable.
        _ = parts.port
        host = parts.hostname
    except ValueError:
        raise InvalidPastedUrl() from None
    if parts.scheme.lower() != "http":
        raise InvalidPastedUrl()
    if "@" in parts.netloc:
        raise InvalidPastedUrl()
    if not host or host.lower() not in {h.lower() for h in allowed_hosts}:
        raise InvalidPastedUrl()

    try:
        query = parse_qs(parts.query, max_num_fields=_MAX_QUERY_FIELDS)
        fragment = parse_qs(parts.fragment, max_num_fields=_MAX_QUERY_FIELDS)
    except ValueError:
        raise InvalidPastedUrl() from None
    if not query:
        if "code" in fragment or "error" in fragment:
            raise FragmentOnlyResponse()
        raise InvalidPastedUrl()

    out: dict[str, Optional[str]] = {}
    for key in _PASTED_KEYS:
        values = query.get(key)
        if values is None:
            out[key] = None
        elif len(values) != 1:
            # A repeated code/state is parameter pollution: which one wins differs between parsers.
            raise InvalidPastedUrl()
        else:
            out[key] = values[0][:MAX_FIELD_LENGTH]
    if not out["code"] and not out["error"]:
        if "code" in fragment or "error" in fragment:
            raise FragmentOnlyResponse()
        raise InvalidPastedUrl()
    if not out["state"]:
        raise InvalidPastedUrl()
    return out


def build_callback_url(public_base_url: str, provider: str) -> str:
    """
    Build the redirect URI registered with the provider from the configured PUBLIC_BASE_URL.

    Never derived from a request's Host header: that header is attacker-controlled and would let a
    forged request choose where the provider sends the code. https only, because both providers
    reject plain http for a non-loopback redirect.

    :raises InvalidCallbackConfig: not an https URL with a host, carries userinfo/query/fragment, or the
        provider is not google or microsoft.
    """
    if provider not in PROVIDERS or not isinstance(public_base_url, str):
        raise InvalidCallbackConfig()
    try:
        parts = urlsplit(public_base_url.strip())
        _ = parts.port
    except ValueError:
        raise InvalidCallbackConfig() from None
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise InvalidCallbackConfig()
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise InvalidCallbackConfig()
    return f"{public_base_url.strip().rstrip('/')}/api/oauth/callback/{provider}"
