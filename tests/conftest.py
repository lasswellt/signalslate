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

CONFIG_PREFIXES = ("M365_", "SLACK_", "ZOOM_", "MSTODO_", "GMAIL_", "DOMAINS_", "JOBS_", "PACKAGES_", "UPS_", "USPS_")
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


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory):
    """One SQLite file with the full schema, created once; every test gets a copy (copying a small
    file is far cheaper than running create_all() 3,000+ times)."""
    from sqlmodel import SQLModel, create_engine

    path = tmp_path_factory.mktemp("schema") / "template.db"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch, tmp_path_factory, _schema_template):
    """
    Every test gets its own SQLite file with the schema in place, and config.json beside it, in a
    directory of its own (not tmp_path, which some tests assert is empty).

    Without this, a test that reaches pipeline.db through known_sources() -> zoom_declared() ->
    connections.get() without patching the engine itself (test_dry_run, test_runner_only, parts of
    test_runner) ran against the repository's real data/digest.db: it passed only when an earlier
    test or process happened to create the tables there, failed with "no such table: connection"
    on a clean checkout, and left data/digest.db, data/config.json and data/jobs/ behind. A test
    that wants a specific engine still monkeypatches db.engine itself; that patch wins inside the
    test and this one is restored after it. Modules that bind DB_PATH by name at import
    (api.routers.job_apply, pipeline.jobs.packet) are patched too when already imported; when they
    are imported later, inside the test, they bind the patched value.
    """
    import shutil
    import sys

    from sqlmodel import create_engine

    from pipeline import config_store, db

    root = tmp_path_factory.mktemp("isolated")
    db_path = root / "isolated.db"
    shutil.copyfile(_schema_template, db_path)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(config_store, "CONFIG_PATH", root / "config.json")
    for name in ("api.routers.job_apply", "pipeline.jobs.packet"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "DB_PATH"):
            monkeypatch.setattr(module, "DB_PATH", db_path)
    yield
    engine.dispose()


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
