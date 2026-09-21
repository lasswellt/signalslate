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
"""
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUIRED_HEADER = "X-Requested-With"
REQUIRED_HEADER_VALUE = "signalslate"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


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
