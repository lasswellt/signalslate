"""
Request guards for an API that has no login.

Design decisions (docs/_research/2026-09-21_management-ui.md sections 8 and 9, verified by experiment):

- CORS alone does not stop a write. A strict CORS config only makes the browser withhold the
  response; a simple cross-site POST still runs the handler. So the server itself rejects a
  non-safe request that lacks the custom header (which forces a preflight from any other origin)
  or that carries an Origin outside the allow-list. Body-less POSTs (run now, test connection) are
  the exposed ones, hence the header requirement applies whether or not there is a body.
- Bodies are JSON only. A form or text/plain body is the CSRF-friendly "simple request" shape and
  also the shape that made FastAPI echo raw input into its error response.
- FastAPI's default 422 echoes the submitted value in `input` (a secret in a too-short password
  field, a whole-body echo on a model_validator failure). hide_input_in_errors does not help under
  FastAPI, so the handler here rebuilds each error from {type, loc, msg} and never touches exc.body.
- The sign-in flow needs cookies across origins: POST /oauth/{provider}/start sets the ss_oauth_<flow_id>
  nonce cookie and paste/callback require it, and web (:3000) and API (:8000) are same-site but
  cross-origin. The browser stores and sends it only with credentials 'include' AND a response carrying
  Access-Control-Allow-Credentials: true, so CORS allows credentials. That is safe only because
  allow_origins is the explicit normalized list: Starlette echoes the exact request origin for a listed
  one and nothing for any other, and a "*" entry is dropped before it gets here. Never let a wildcard,
  or a reflected arbitrary Origin, reach that list while credentials are allowed.
- health.web_origins() returns the operator's strings verbatim, so a pasted "https://Host/" would
  never equal the canonical Origin a browser sends. Both sides are normalized before comparing.
- Origin and header checks only stop writes. With no login, a DNS-rebinding page (attacker.example
  re-resolved to this machine's address) makes the browser send same-origin GETs to the API and lets
  its script READ collected items, config and status. The browser still sends Host: attacker.example,
  so install_host_guard refuses every request whose Host is not on an explicit allow-list, on every
  path and method (no /api/health exemption: a health probe reaches the API by a listed name).
"""
import ipaddress
import re
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

REQUIRED_HEADER = "X-Requested-With"
REQUIRED_HEADER_VALUE = "signalslate"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOSTNAME = re.compile(r"[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?")
_PORT = re.compile(r"[0-9]{1,5}")


def normalize_origin(value: str) -> str | None:
    """
    Canonical "scheme://host[:port]" form of an origin, or None when it can never be a browser Origin.

    Lowercases scheme and host, drops default ports and trailing slashes. None covers "null" (an
    opaque origin), a "*" wildcard, non-http(s) schemes, userinfo, and any path, query or fragment.

    :param value: an Origin header value or a configured allowed origin.
    :returns: the normalized origin, or None if it is not a usable http(s) origin.
    """
    text = value.strip().rstrip("/")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if "@" in parts.netloc or parts.path or parts.query or parts.fragment:
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def normalize_host(value: str) -> str | None:
    """
    Canonical lowercase hostname of a Host header value or a configured host, or None if unusable.

    Accepts "name", "name:port", "1.2.3.4[:port]", "[::1][:port]" and a bare "::1" (configured
    entries only mean that; a request never sends it, and the guard treats it the same way). The
    port is validated and then ignored, because the same API answers on several ports behind
    different proxies. None covers an empty value, a "*" wildcard or any other character outside a
    hostname or an IP literal (userinfo, path, a scheme, spaces), and a malformed port.

    :param value: a Host header value or a configured allowed host.
    :returns: the lowercase hostname without brackets or port, or None.
    """
    text = value.strip().lower()
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        host, rest = text[1:end], text[end + 1:]
        if rest and not (rest.startswith(":") and _PORT.fullmatch(rest[1:])):
            return None
        return host if _is_ipv6(host) else None
    if text.count(":") > 1:
        return text if _is_ipv6(text) else None
    host, _, port = text.partition(":")
    if ":" in text and not _PORT.fullmatch(port):
        return None
    return host if _HOSTNAME.fullmatch(host) else None


def _is_ipv6(host: str) -> bool:
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return False
    return True


def _carries_body(headers: Headers) -> bool:
    if "transfer-encoding" in headers:
        return True
    length = headers.get("content-length")
    return length is not None and length.strip() != "0"


def _is_json(content_type: str | None) -> bool:
    if content_type is None:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


class _CredentialedCORS(CORSMiddleware):
    """
    CORSMiddleware that never advertises credentials to an origin it does not allow.

    Starlette stamps Access-Control-Allow-Credentials on every simple response and preflight once
    allow_credentials is on, allowed origin or not. Without an Allow-Origin the browser discards the
    response anyway, but a stray "credentials true" on an unlisted origin's answer is the wrong signal
    to leave for any client or test that reads the headers, so it is removed unless an allowed origin
    was echoed.
    """

    async def send(self, message: Message, send: Send, request_headers: Headers) -> None:
        if message["type"] == "http.response.start":
            async def _send(inner: Message) -> None:
                if inner["type"] == "http.response.start":
                    _drop_credentials_without_origin(MutableHeaders(scope=inner))
                await send(inner)

            await super().send(message, _send, request_headers)
            return
        await super().send(message, send, request_headers)

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        _drop_credentials_without_origin(response.headers)
        return response


def _drop_credentials_without_origin(headers: MutableHeaders) -> None:
    if "access-control-allow-origin" not in headers:
        del headers["access-control-allow-credentials"]


class _RequestGuard:
    """Pure ASGI middleware: no body buffering, and it runs before routing so no handler executes on a reject."""

    def __init__(self, app: ASGIApp, allowed_origins: frozenset[str]) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        rejection: JSONResponse | None = None
        if headers.get(REQUIRED_HEADER) != REQUIRED_HEADER_VALUE:
            rejection = JSONResponse({"detail": f"{REQUIRED_HEADER} header required"}, status_code=403)
        elif "origin" in headers and normalize_origin(headers["origin"]) not in self.allowed_origins:
            rejection = JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        elif _carries_body(headers) and not _is_json(headers.get("content-type")):
            rejection = JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)

        if rejection is not None:
            await rejection(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _HostGuard:
    """
    Pure ASGI middleware: refuses any HTTP or WebSocket request whose Host is not allow-listed.

    A missing, duplicated or malformed Host is refused too; a request that lands here without one
    is not a browser. The submitted value is never echoed back or logged: it is attacker-chosen.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        values = Headers(scope=scope).getlist("host")
        host = normalize_host(values[0]) if len(values) == 1 else None
        if host is not None and host in self.allowed_hosts:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        rejection = JSONResponse(
            {"detail": {"code": "host_not_allowed", "message": "Host not allowed"}}, status_code=400
        )
        await rejection(scope, receive, send)


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    # Only these three keys: `input` echoes the submitted value, `ctx` can embed it, `url` is noise.
    errors = [
        {"type": err["type"], "loc": list(err["loc"]), "msg": err["msg"]}
        for err in exc.errors()
    ]
    return JSONResponse({"detail": errors}, status_code=422)


def install_security(app: FastAPI, *, allowed_origins: list[str]) -> None:
    """
    Install CORS, the mutating-request guard and the non-echoing 422 handler on `app`.

    Call once, before the app starts. CORS is added last so it is the outermost layer: a preflight
    is answered before the guard, and a guard 403 still carries CORS headers for an allowed origin,
    which lets the UI read the error instead of seeing an opaque network failure.

    :param app: the FastAPI app to protect.
    :param allowed_origins: browser origins allowed to write, e.g. health.web_origins(). Entries are
        normalized; unusable ones (a "*" wildcard, non-http schemes) are dropped, never widened.
    """
    normalized = sorted(
        {origin for origin in (normalize_origin(raw) for raw in allowed_origins) if origin is not None}
    )
    app.add_middleware(_RequestGuard, allowed_origins=frozenset(normalized))
    app.add_middleware(
        _CredentialedCORS,
        allow_origins=normalized,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", REQUIRED_HEADER],
        allow_credentials=True,
    )
    app.add_exception_handler(RequestValidationError, _validation_error_handler)


def install_host_guard(app: FastAPI, *, allowed_hosts: list[str]) -> None:
    """
    Refuse every request whose Host header is not in `allowed_hosts` (DNS-rebinding defence).

    Call after install_security so it is the outermost layer: nothing, not even a CORS preflight,
    is answered for an unlisted Host. A rejection is 400 {"detail": {"code": "host_not_allowed", ...}}
    with no CORS headers, since a rebinding page is not an allowed origin anyway.

    :param app: the FastAPI app to protect.
    :param allowed_hosts: hostnames (or host:port, the port is ignored) the API may be reached by.
        Compared case-insensitively; IPv6 literals may be written with or without brackets. Unusable
        entries, including any "*" wildcard, are dropped, never widened: an empty result refuses everything.
    """
    normalized = frozenset(
        host for host in (normalize_host(raw) for raw in allowed_hosts) if host is not None
    )
    app.add_middleware(_HostGuard, allowed_hosts=normalized)
