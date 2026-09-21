"""
The api container must not write OAuth callback codes to its access log.

uvicorn's default access log prints the whole request line, query string included, so
GET /api/oauth/callback/<provider>?state=...&code=... would put the authorization code and the
state into `docker logs` (check-report finding 5). The fix is --no-access-log in api/Dockerfile's
CMD. Two layers of proof:

1. The CMD is parsed out of the Dockerfile and carries the flag with host and port intact.
2. A real uvicorn server runs on 127.0.0.1 with the access_log value that uvicorn's OWN CLI parser
   derives from that CMD, a request carrying code and state goes through it, and the uvicorn.access
   logger is captured. A control run with access_log=True must log the query string: without it a
   green result could just mean the capture never worked.

A scratch app is used, never api.main: its lifespan opens the real database.
"""
import http.client
import json
import logging
import re
import socket
import threading
from pathlib import Path
from typing import List

import pytest
import uvicorn
from fastapi import FastAPI
from uvicorn.main import main as uvicorn_cli

DOCKERFILE = Path(__file__).resolve().parent.parent / "api" / "Dockerfile"

CODE = "SECRETVALUE"
STATE = "STATEVALUE"
QUERY = f"code={CODE}&state={STATE}"


def _cmd_argv() -> List[str]:
    """The Dockerfile's CMD as an argv list (exec form, a JSON array)."""
    matches = re.findall(r"^CMD\s+(\[.*\])\s*$", DOCKERFILE.read_text(), re.MULTILINE)
    assert len(matches) == 1, "api/Dockerfile must have exactly one exec-form CMD"
    argv = json.loads(matches[0])
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    return argv


class _Capture(logging.Filter):
    """Collects formatted messages of uvicorn.access records.

    A Filter, not a Handler: uvicorn logs an access line only when
    logging.getLogger("uvicorn.access").hasHandlers() is true, and --no-access-log works by
    emptying that handler list. Attaching a capturing Handler would switch logging back on and
    make this experiment lie. The logger does not propagate, so caplog cannot see it either.
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: List[str] = []

    def filter(self, record: logging.LogRecord) -> bool:
        self.messages.append(record.getMessage())
        return True


def _scratch_app() -> FastAPI:
    app = FastAPI()

    @app.get("/callback")
    def callback() -> dict:
        return {"ok": True}

    return app


def _get_through_uvicorn(access_log: bool) -> List[str]:
    """Serve the scratch app on an ephemeral 127.0.0.1 port, GET it with code and state, and
    return every message the uvicorn.access logger emitted."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        _scratch_app(), host="127.0.0.1", port=port, access_log=access_log, log_level="info"
    )
    server = uvicorn.Server(config)
    # Attached after Config(): Config applies the default logging dict on construction and
    # would otherwise reset the logger.
    capture = _Capture()
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.addFilter(capture)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if server.started:
                break
            thread.join(0.01)
        assert server.started, "uvicorn did not start"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", f"/callback?{QUERY}")
            resp = conn.getresponse()
            assert resp.status == 200
            resp.read()
        finally:
            conn.close()
    finally:
        server.should_exit = True
        thread.join(10)
        access_logger.removeFilter(capture)
        sock.close()
    assert not thread.is_alive(), "uvicorn did not stop"
    return capture.messages


@pytest.fixture
def dockerfile_argv() -> List[str]:
    return _cmd_argv()


def test_cmd_disables_access_log_and_keeps_host_and_port(dockerfile_argv: List[str]) -> None:
    assert dockerfile_argv[0] == "uvicorn"
    assert "api.main:app" in dockerfile_argv
    assert "--no-access-log" in dockerfile_argv
    assert dockerfile_argv[dockerfile_argv.index("--host") + 1] == "0.0.0.0"
    assert dockerfile_argv[dockerfile_argv.index("--port") + 1] == "8000"


def test_uvicorn_cli_reads_the_cmd_as_access_log_off(dockerfile_argv: List[str]) -> None:
    ctx = uvicorn_cli.make_context("uvicorn", dockerfile_argv[1:])
    assert ctx.params["access_log"] is False
    assert ctx.params["host"] == "0.0.0.0"
    assert ctx.params["port"] == 8000


def test_callback_code_and_state_never_reach_the_access_log(dockerfile_argv: List[str]) -> None:
    access_log = uvicorn_cli.make_context("uvicorn", dockerfile_argv[1:]).params["access_log"]
    messages = _get_through_uvicorn(access_log=access_log)
    joined = "\n".join(messages)
    assert CODE not in joined
    assert STATE not in joined
    assert messages == []


def test_control_default_access_log_does_leak_the_query() -> None:
    """Proves the capture above can see a leak: with access_log on, the query string is logged."""
    messages = _get_through_uvicorn(access_log=True)
    joined = "\n".join(messages)
    assert CODE in joined
    assert STATE in joined
