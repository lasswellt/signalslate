"""
Shared fixtures.

The important one is autouse: pipeline.health._env() merges the process environment over .env so
the deployed container can see its config, which means a developer with the real .env sourced — or
a CI runner injecting these as secrets — would otherwise have their environment silently override
what a test wrote to its temp .env. Seven tests failed that way before this existed.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CONFIG_PREFIXES = ("M365_", "SLACK_", "ZOOM_", "MSTODO_", "GMAIL_", "DOMAINS_")
CONFIG_KEYS = (
    "RMAPI_CONFIG", "LAN_HOST", "TZ", "ANTHROPIC_API_KEY", "SIGNALSLATE_MAP_MODEL",
    "SIGNALSLATE_SECRET_KEY", "WEB_ORIGINS", "PUBLIC_BASE_URL", "ALLOWED_HOSTS",
)

# Starlette's TestClient sends Host: testserver. api.main builds its Host guard at import time,
# which for test modules is collection time, before any fixture runs, so the value has to be in
# the process environment by then. The autouse fixture below keeps it for every test but lets a
# test override it, and restores it afterwards.
TEST_ALLOWED_HOSTS = "testserver"
os.environ["ALLOWED_HOSTS"] = TEST_ALLOWED_HOSTS


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Strip every SignalSlate config variable from the environment for the duration of a test."""
    for key in list(os.environ):
        if key.startswith(CONFIG_PREFIXES) or key in CONFIG_KEYS:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALLOWED_HOSTS", TEST_ALLOWED_HOSTS)


@pytest.fixture(autouse=True)
def no_env_overlay():
    """
    The overlay provider is process-global state that startup registers; a test (or an app started
    inside one) that leaves it registered would change what every later test's _env() returns.
    """
    from pipeline import health

    health.set_env_overlay_provider(None, None)
    yield
    health.set_env_overlay_provider(None, None)
