"""
Tests for the env seam in pipeline.health: the registered overlay, the per-context snapshot and
override, the new settings accessors, and per-label Gmail client credentials.

_env() is the one choke point every collector and health check reads, so the first tests pin the
default: with nothing registered, behavior is exactly today's. The overlay is process-global; the
autouse no_env_overlay fixture in conftest.py clears it around every test.
"""
import sys
import threading
from pathlib import Path
from typing import Mapping, Optional

import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, crypto, db, health  # noqa: E402


@pytest.fixture
def dotenv(monkeypatch, tmp_path):
    """Points health.ROOT at a tmp dir and returns a writer for its .env."""
    monkeypatch.setattr(health, "ROOT", tmp_path)

    def _write(contents: str) -> None:
        (tmp_path / ".env").write_text(contents)

    return _write


class StubOverlay:
    """A provider whose contents the test can change between calls, as a UI edit would."""

    def __init__(self, keys: Optional[Mapping[str, str]]) -> None:
        self.keys = keys

    def __call__(self) -> Optional[Mapping[str, str]]:
        return self.keys


def is_family(name: str) -> bool:
    return name.startswith("SLACK_") and name.endswith("_TOKEN")


# --- overlay seam -------------------------------------------------------------------


def test_overlay_off_by_default_matches_raw_env(dotenv, monkeypatch):
    dotenv("SLACK_WORK_TOKEN=xoxp-file\nTZ=UTC\n")
    monkeypatch.setenv("SLACK_HOME_TOKEN", "xoxp-process")
    assert health._env() == health._raw_env()
    assert health.env() == health._raw_env()
    assert health.slack_workspaces() == {"home": "xoxp-process", "work": "xoxp-file"}


def test_overlay_replaces_family_keys_and_adds_its_own(dotenv, monkeypatch):
    dotenv("SLACK_WORK_TOKEN=xoxp-file\nTZ=UTC\n")
    monkeypatch.setenv("SLACK_HOME_TOKEN", "xoxp-process")
    health.set_env_overlay_provider(StubOverlay({"SLACK_NEW_TOKEN": "xoxp-store"}), is_family)

    env = health._env()

    assert "SLACK_WORK_TOKEN" not in env
    assert "SLACK_HOME_TOKEN" not in env
    assert env["SLACK_NEW_TOKEN"] == "xoxp-store"
    assert health.slack_workspaces() == {"new": "xoxp-store"}


def test_overlay_drops_a_deleted_connection_still_in_os_environ(dotenv, monkeypatch):
    dotenv("")
    monkeypatch.setenv("SLACK_GONE_TOKEN", "xoxp-stale")
    health.set_env_overlay_provider(StubOverlay({"SLACK_KEPT_TOKEN": "xoxp-kept"}), is_family)

    assert health.slack_workspaces() == {"kept": "xoxp-kept"}
    assert health._raw_env()["SLACK_GONE_TOKEN"] == "xoxp-stale"


def test_overlay_leaves_non_family_keys_untouched(dotenv, monkeypatch):
    dotenv("SLACK_SKIP_DMS=true\nLAN_HOST=lan.example.com\n")
    monkeypatch.setenv("TZ", "Europe/Paris")
    health.set_env_overlay_provider(StubOverlay({"SLACK_A_TOKEN": "xoxp-a"}), is_family)

    env = health._env()

    assert env["SLACK_SKIP_DMS"] == "true"
    assert env["LAN_HOST"] == "lan.example.com"
    assert env["TZ"] == "Europe/Paris"
    assert health.env_flag("SLACK_SKIP_DMS") is True


def test_provider_returning_none_leaves_env_untouched(dotenv, monkeypatch):
    dotenv("SLACK_WORK_TOKEN=xoxp-file\n")
    monkeypatch.setenv("SLACK_HOME_TOKEN", "xoxp-process")
    health.set_env_overlay_provider(StubOverlay(None), is_family)

    assert health._env() == health._raw_env()
    assert set(health.slack_workspaces()) == {"home", "work"}


def test_overlay_accepts_any_mapping_and_never_mutates_it(dotenv):
    from types import MappingProxyType

    dotenv("")
    shared = MappingProxyType({"SLACK_A_TOKEN": "xoxp-a"})
    health.set_env_overlay_provider(lambda: shared, is_family)

    health._env()["SLACK_B_TOKEN"] = "xoxp-b"

    assert dict(shared) == {"SLACK_A_TOKEN": "xoxp-a"}
    assert "SLACK_B_TOKEN" not in health._env()


def test_clearing_the_provider_restores_raw_behavior(dotenv):
    dotenv("SLACK_WORK_TOKEN=xoxp-file\n")
    health.set_env_overlay_provider(StubOverlay({}), is_family)
    assert health.slack_workspaces() == {}

    health.set_env_overlay_provider(None, None)

    assert health.slack_workspaces() == {"work": "xoxp-file"}


# --- snapshot and override ----------------------------------------------------------


def test_snapshot_freezes_across_a_mid_run_overlay_change(dotenv):
    dotenv("")
    overlay = StubOverlay({"SLACK_A_TOKEN": "xoxp-a"})
    health.set_env_overlay_provider(overlay, is_family)

    with health.env_snapshot():
        overlay.keys = {"SLACK_B_TOKEN": "xoxp-b"}
        assert health.slack_workspaces() == {"a": "xoxp-a"}
        with health.env_snapshot():  # nested: the outer view stays
            assert health.slack_workspaces() == {"a": "xoxp-a"}
        assert health.slack_workspaces() == {"a": "xoxp-a"}

    assert health.slack_workspaces() == {"b": "xoxp-b"}


def test_snapshot_hands_out_copies(dotenv):
    dotenv("TZ=UTC\n")
    with health.env_snapshot():
        health._env()["TZ"] = "Mars/Olympus"
        assert health._env()["TZ"] == "UTC"


def test_env_override_layers_over_current_and_restores(dotenv):
    dotenv("ZOOM_CLIENT_ID=saved\nTZ=UTC\n")

    with health.env_override({"ZOOM_CLIENT_ID": "candidate", "ZOOM_ACCOUNT_ID": "acct"}):
        env = health._env()
        assert env["ZOOM_CLIENT_ID"] == "candidate"
        assert env["ZOOM_ACCOUNT_ID"] == "acct"
        assert env["TZ"] == "UTC"
        with health.env_override({"ZOOM_ACCOUNT_ID": "inner"}):
            assert health._env()["ZOOM_ACCOUNT_ID"] == "inner"
            assert health._env()["ZOOM_CLIENT_ID"] == "candidate"
        assert health._env()["ZOOM_ACCOUNT_ID"] == "acct"

    env = health._env()
    assert env["ZOOM_CLIENT_ID"] == "saved"
    assert "ZOOM_ACCOUNT_ID" not in env


def test_env_override_layers_over_a_frozen_snapshot_and_restores(dotenv):
    dotenv("")
    overlay = StubOverlay({"SLACK_A_TOKEN": "xoxp-a"})
    health.set_env_overlay_provider(overlay, is_family)

    with health.env_snapshot():
        overlay.keys = {"SLACK_B_TOKEN": "xoxp-b"}
        with health.env_override({"SLACK_C_TOKEN": "xoxp-c"}):
            assert health.slack_workspaces() == {"a": "xoxp-a", "c": "xoxp-c"}
        assert health.slack_workspaces() == {"a": "xoxp-a"}


def test_override_is_restored_when_the_body_raises(dotenv):
    dotenv("TZ=UTC\n")
    with pytest.raises(ValueError):
        with health.env_override({"TZ": "Mars/Olympus"}):
            raise ValueError("boom")
    assert health._env()["TZ"] == "UTC"


def test_snapshot_and_override_are_isolated_between_threads(dotenv):
    dotenv("")
    overlay = StubOverlay({"SLACK_A_TOKEN": "xoxp-a"})
    health.set_env_overlay_provider(overlay, is_family)
    entered = threading.Event()
    changed = threading.Event()
    seen: dict[str, dict] = {}

    def holder() -> None:
        with health.env_snapshot(), health.env_override({"TZ": "Mars/Olympus"}):
            entered.set()
            changed.wait(timeout=5)
            seen["holder"] = health._env()

    def bystander() -> None:
        entered.wait(timeout=5)
        overlay.keys = {"SLACK_B_TOKEN": "xoxp-b"}
        seen["bystander"] = health._env()
        changed.set()

    threads = [threading.Thread(target=holder), threading.Thread(target=bystander)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert seen["holder"]["SLACK_A_TOKEN"] == "xoxp-a"
    assert "SLACK_B_TOKEN" not in seen["holder"]
    assert seen["holder"]["TZ"] == "Mars/Olympus"
    assert seen["bystander"]["SLACK_B_TOKEN"] == "xoxp-b"
    assert "SLACK_A_TOKEN" not in seen["bystander"]
    assert "TZ" not in seen["bystander"]
    assert "SLACK_B_TOKEN" in health._env()  # and this thread never entered either


# --- settings accessors -------------------------------------------------------------


def test_secret_key_setting(dotenv, monkeypatch):
    assert health.secret_key_setting() is None
    dotenv("SIGNALSLATE_SECRET_KEY=   \n")
    assert health.secret_key_setting() is None
    monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", " key-one,key-two ")
    assert health.secret_key_setting() == "key-one,key-two"


def test_web_origins_default_is_localhost_only(dotenv):
    dotenv("")
    assert health.web_origins() == ["http://localhost:3000"]


def test_web_origins_default_adds_lan_host(dotenv):
    dotenv("LAN_HOST=lan.example.com\n")
    assert health.web_origins() == ["http://localhost:3000", "http://lan.example.com:3000"]


def test_web_origins_splits_on_commas_and_drops_blanks(dotenv):
    dotenv("WEB_ORIGINS= https://a.example.com , ,https://b.example.org,\nLAN_HOST=lan.example.com\n")
    assert health.web_origins() == ["https://a.example.com", "https://b.example.org"]


def test_web_origins_blank_value_counts_as_unset(dotenv):
    dotenv("WEB_ORIGINS= , \n")
    assert health.web_origins() == ["http://localhost:3000"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://signalslate.example.com", "https://signalslate.example.com"),
        ("https://signalslate.example.com/", "https://signalslate.example.com"),
        ("  https://signalslate.example.com/base//  ", "https://signalslate.example.com/base"),
        ("http://signalslate.example.com", None),
        ("signalslate.example.com", None),
        ("https://", None),
        ("", None),
    ],
)
def test_public_base_url_is_https_only_with_no_trailing_slash(dotenv, raw, expected):
    dotenv(f"PUBLIC_BASE_URL={raw}\n")
    assert health.public_base_url() == expected


def test_public_base_url_unset_is_none(dotenv):
    dotenv("")
    assert health.public_base_url() is None


def test_new_settings_are_read_from_the_process_environment(dotenv, monkeypatch):
    dotenv("")
    monkeypatch.setenv("WEB_ORIGINS", "https://ui.example.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://ui.example.com/")
    monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", "k")
    assert health.web_origins() == ["https://ui.example.com"]
    assert health.public_base_url() == "https://ui.example.com"
    assert health.secret_key_setting() == "k"


# --- per-label Gmail client credentials ---------------------------------------------


class _FakeTokenResponse:
    status_code = 200

    def json(self):
        return {"access_token": "at", "scope": health.GMAIL_SCOPE}

    def raise_for_status(self):
        return None


def _capture_token_post(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    def fake_post(url, data=None, timeout=None, **kwargs):
        sent.append(dict(data or {}))
        return _FakeTokenResponse()

    monkeypatch.setattr(health.requests, "post", fake_post)
    return sent


def test_gmail_prefers_per_label_client_over_shared_pair(dotenv, monkeypatch):
    dotenv(
        "GMAIL_CLIENT_ID=shared-id\nGMAIL_CLIENT_SECRET=shared-secret\n"
        "GMAIL_HOME_REFRESH_TOKEN=rt-home\n"
        "GMAIL_HOME_CLIENT_ID=home-id\nGMAIL_HOME_CLIENT_SECRET=home-secret\n"
        "GMAIL_WORK_REFRESH_TOKEN=rt-work\n"
    )
    sent = _capture_token_post(monkeypatch)

    health.gmail_token_response("home")
    health.gmail_token_response("work")

    assert (sent[0]["client_id"], sent[0]["client_secret"], sent[0]["refresh_token"]) == ("home-id", "home-secret", "rt-home")
    assert (sent[1]["client_id"], sent[1]["client_secret"], sent[1]["refresh_token"]) == ("shared-id", "shared-secret", "rt-work")


def test_gmail_per_label_pair_alone_needs_no_shared_pair(dotenv, monkeypatch):
    dotenv("GMAIL_HOME_REFRESH_TOKEN=rt\nGMAIL_HOME_CLIENT_ID=home-id\nGMAIL_HOME_CLIENT_SECRET=home-secret\n")
    sent = _capture_token_post(monkeypatch)

    health.gmail_token_response("home")

    assert sent[0]["client_id"] == "home-id"


def test_gmail_missing_credentials_message_is_unchanged(dotenv):
    dotenv("GMAIL_HOME_REFRESH_TOKEN=rt\n")
    with pytest.raises(RuntimeError, match=r"^Missing GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in \.env$"):
        health.gmail_token_response("home")


# --- registered with the real connections module ------------------------------------


@pytest.fixture
def vault(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    yield
    connections.set_vault(None)


def test_real_connections_provider_and_family_test_agree(dotenv, monkeypatch, vault):
    dotenv("SLACK_WORK_TOKEN=xoxp-file\nSLACK_SKIP_DMS=true\nTZ=UTC\n")
    monkeypatch.setenv("SLACK_GONE_TOKEN", "xoxp-stale")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "shared-id.example.com")
    connections.create("slack", {"label": "store", "token": "xoxp-store"})
    connections.create(
        "gmail",
        {"label": "home", "client_id": "home-id.example.com", "client_secret": "home-secret", "refresh_token": "rt-home"},
    )
    health.set_env_overlay_provider(connections.overlay_provider, connections.is_family_key)

    env = health._env()

    assert health.slack_workspaces() == {"store": "xoxp-store"}
    assert "GMAIL_CLIENT_ID" not in env  # a family key the store owns: the raw shared id is gone
    assert env["GMAIL_HOME_CLIENT_ID"] == "home-id.example.com"
    assert env["SLACK_SKIP_DMS"] == "true"  # not a family key: survives
    assert env["TZ"] == "UTC"
    # Every key the store materialized is one the family test claims, and every raw key it
    # dropped was too: the two halves cannot drift apart unnoticed.
    overlay = connections.overlay_provider()
    assert overlay is not None
    assert all(connections.is_family_key(key) for key in overlay)
    dropped = set(health._raw_env()) - set(env)
    assert dropped and all(connections.is_family_key(key) for key in dropped)
    kept = set(health._raw_env()) & set(env)
    assert not any(connections.is_family_key(key) for key in kept)
    assert health.gmail_accounts() == {"home": "rt-home"}


def test_no_vault_means_the_real_provider_leaves_env_alone(dotenv, monkeypatch, vault):
    dotenv("SLACK_WORK_TOKEN=xoxp-file\n")
    connections.set_vault(None)
    health.set_env_overlay_provider(connections.overlay_provider, connections.is_family_key)

    assert health._env() == health._raw_env()
    assert health.slack_workspaces() == {"work": "xoxp-file"}
