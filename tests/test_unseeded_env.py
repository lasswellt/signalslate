"""
A .env source that fails seeding keeps working instead of vanishing (check finding 3).

Once a key is set, health._live_env drops every family key of the raw env, so a declaration the
seeding rejected (an M365 alias with an underscore, a Gmail account with no client credentials)
would have silently stopped being collected. These tests run the REAL lifespan against a temp
database and a temp .env, exactly like tests/test_api_startup.py, and assert that:

- a rejected declaration still resolves through the health helpers and is named (by key, never by
  value) in GET /system's unseeded_env;
- the store still wins: a tombstone or a stored row ends the retention;
- nothing changes without a key;
- no retained value reaches a response, a log record or captured output.
"""
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.main as api_main  # noqa: E402
from pipeline import config_store, connections, crypto, db, health  # noqa: E402

WRITE = {"X-Requested-With": "signalslate"}

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SHARED_CLIENT = "99999999-8888-7777-6666-555555555555"
SLACK_TOKEN = "xoxb-unseeded-test-Hd91XvB7mR"
GMAIL_REFRESH = "1//unseeded-refresh-Pq83LmZk27"
GMAIL_CLIENT_ID = "unseeded-gmail-client.apps.example.com"
GMAIL_CLIENT_SECRET = "unseeded-gmail-secret-Vt62Kd"
RETAINED_VALUES = (TENANT, CLIENT, SHARED_CLIENT, GMAIL_REFRESH, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET)

# The underscore is refused by the alias rule (letters, digits and hyphens), but the source ran fine
# from .env before the store existed, so it must keep running.
BAD_ALIAS_ENV = (
    "M365_ORG1_ALIAS=my_org\n"
    f"M365_ORG1_TENANT_ID={TENANT}\n"
    f"M365_ORG1_CLIENT_ID={CLIENT}\n"
    f"SLACK_WORK_TOKEN={SLACK_TOKEN}\n"
)


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Temp DB, temp ROOT (so .env is ours), temp config file, no-op scheduler; store state reset after."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(api_main, "start_scheduler", lambda: None)
    (tmp_path / ".env").write_text(BAD_ALIAS_ENV)
    yield tmp_path
    connections.set_vault(None)
    health.set_env_overlay_provider(None, None)


@contextmanager
def running_app() -> Iterator[TestClient]:
    with TestClient(api_main.app, headers=WRITE) as client:
        yield client


def _with_key(monkeypatch) -> str:
    key = crypto.generate_key()
    monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", key)
    return key


def _set_env(home: Path, text: str) -> None:
    (home / ".env").write_text(text)


def _assert_no_retained_value(text: str) -> None:
    for value in (*RETAINED_VALUES, SLACK_TOKEN):
        assert value not in text


def test_a_rejected_m365_alias_keeps_running_and_slack_is_seeded(home, monkeypatch, caplog, capsys):
    key = _with_key(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        with running_app() as client:
            assert "my_org" not in {row["label"] for row in client.get("/api/connections").json()}
            assert [row["id"] for row in client.get("/api/connections").json()] == ["slack_work"]

            # The declaration is gone from the raw env's point of view: the store owns the family.
            _set_env(home, "")
            assert set(health.known_sources()) == {"m365_my_org", "zoom", "slack_work"}
            assert health.m365_tenant_config("my_org") == {"alias": "my_org", "tenant_id": TENANT, "client_id": CLIENT}

            system = client.get("/api/system")
            assert system.json()["unseeded_env"] == ["M365_ORG1_ALIAS", "M365_ORG1_CLIENT_ID", "M365_ORG1_TENANT_ID"]
            assert system.json()["store_active"] is True
            # Every existing /system field is still there.
            assert {"secret_key_configured", "public_base_url_configured", "web_origins", "oauth"} <= set(system.json())

            collectors = client.get("/api/collectors")
            assert "m365_my_org" in {row["source"] for row in collectors.json()}
            for response in (system, collectors, client.get("/api/connections"), client.get("/api/status")):
                _assert_no_retained_value(response.text)
                assert key not in response.text

    out = capsys.readouterr().out
    for text in (caplog.text, out):
        _assert_no_retained_value(text)
        assert key not in text
    assert "M365_ORG1_ALIAS" in caplog.text


def test_a_retained_org_is_numbered_after_the_stored_orgs(home, monkeypatch):
    _with_key(monkeypatch)
    _set_env(
        home,
        "M365_ORG1_ALIAS=my_org\n"
        f"M365_ORG1_TENANT_ID={TENANT}\n"
        f"M365_ORG1_CLIENT_ID={CLIENT}\n"
        "M365_ORG2_ALIAS=Good\n"
        "M365_ORG2_TENANT_ID=contoso.example.com\n"
        f"M365_ORG2_CLIENT_ID={CLIENT}\n",
    )
    with running_app():
        _set_env(home, "")
        # Good is stored as org 1; reusing the env number 1 for my_org would overwrite it.
        assert health.m365_aliases() == ["Good", "my_org"]
        assert health.m365_tenant_config("Good") == {"alias": "Good", "tenant_id": "contoso.example.com", "client_id": CLIENT}
        assert health.m365_tenant_config("my_org") == {"alias": "my_org", "tenant_id": TENANT, "client_id": CLIENT}


def test_a_retained_org_keeps_the_shared_client_id(home, monkeypatch):
    _with_key(monkeypatch)
    _set_env(home, f"M365_CLIENT_ID={SHARED_CLIENT}\nM365_ORG1_ALIAS=my_org\nM365_ORG1_TENANT_ID={TENANT}\n")
    with running_app() as client:
        _set_env(home, "")
        assert health.m365_tenant_config("my_org") == {"alias": "my_org", "tenant_id": TENANT, "client_id": SHARED_CLIENT}
        names = client.get("/api/system").json()["unseeded_env"]
        assert names == ["M365_CLIENT_ID", "M365_ORG1_ALIAS", "M365_ORG1_TENANT_ID"]
        _assert_no_retained_value(client.get("/api/system").text)


def test_an_org_missing_its_tenant_id_is_retained_and_still_unresolvable_as_before(home, monkeypatch):
    _with_key(monkeypatch)
    _set_env(home, f"M365_ORG1_ALIAS=NoTenant\nM365_ORG1_CLIENT_ID={CLIENT}\n")
    with running_app() as client:
        _set_env(home, "")
        assert "m365_NoTenant" in health.known_sources()
        assert health.m365_tenant_config("NoTenant") is None
        assert client.get("/api/system").json()["unseeded_env"] == ["M365_ORG1_ALIAS", "M365_ORG1_CLIENT_ID"]


def test_a_gmail_account_without_client_credentials_is_retained_with_its_shared_ones_resolved(home, monkeypatch):
    _with_key(monkeypatch)
    # Only the id is shared, so the account cannot be stored, but it ran (and failed with a clear
    # message) before the key was set and must not change.
    _set_env(home, f"GMAIL_HOME_REFRESH_TOKEN={GMAIL_REFRESH}\nGMAIL_CLIENT_ID={GMAIL_CLIENT_ID}\n")
    with running_app() as client:
        _set_env(home, "")
        assert health.gmail_accounts() == {"home": GMAIL_REFRESH}
        assert health._env()["GMAIL_HOME_CLIENT_ID"] == GMAIL_CLIENT_ID
        assert client.get("/api/system").json()["unseeded_env"] == ["GMAIL_CLIENT_ID", "GMAIL_HOME_REFRESH_TOKEN"]
        assert client.get("/api/connections").json() == []


def test_an_over_long_slack_label_is_retained(home, monkeypatch):
    _with_key(monkeypatch)
    long_key = "SLACK_" + "A" * 40 + "_TOKEN"
    _set_env(home, f"{long_key}={SLACK_TOKEN}\nSLACK_WORK_TOKEN=xoxb-other-Nn41Zx\n")
    with running_app() as client:
        _set_env(home, "")
        assert health.slack_workspaces()["a" * 40] == SLACK_TOKEN
        assert client.get("/api/system").json()["unseeded_env"] == [long_key]
        assert [row["id"] for row in client.get("/api/connections").json()] == ["slack_work"]


def test_a_partial_zoom_declaration_is_retained(home, monkeypatch):
    _with_key(monkeypatch)
    _set_env(home, f"ZOOM_ACCOUNT_ID=acct_Example123\nZOOM_CLIENT_SECRET={GMAIL_CLIENT_SECRET}\n")
    with running_app() as client:
        _set_env(home, "")
        assert health._env()["ZOOM_ACCOUNT_ID"] == "acct_Example123"
        assert client.get("/api/system").json()["unseeded_env"] == ["ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_SECRET"]
        _assert_no_retained_value(client.get("/api/system").text)


def test_a_deleted_seeded_id_is_not_retained(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app() as client:
        assert client.delete("/api/connections/slack_work").status_code == 204
        assert client.get("/api/system").json()["unseeded_env"] == ["M365_ORG1_ALIAS", "M365_ORG1_CLIENT_ID", "M365_ORG1_TENANT_ID"]

    # A new startup on the same database, .env still declaring the deleted workspace.
    with running_app() as client:
        assert "slack_work" not in health.known_sources()
        assert "SLACK_WORK_TOKEN" not in client.get("/api/system").json()["unseeded_env"]


def test_a_tombstoned_id_whose_env_declaration_is_rejected_is_not_retained(home, monkeypatch):
    _with_key(monkeypatch)
    _set_env(home, f"GMAIL_HOME_REFRESH_TOKEN={GMAIL_REFRESH}\nGMAIL_CLIENT_ID={GMAIL_CLIENT_ID}\n")
    with running_app() as client:
        assert client.get("/api/system").json()["unseeded_env"] == ["GMAIL_CLIENT_ID", "GMAIL_HOME_REFRESH_TOKEN"]
        connections.create("gmail", {"label": "home", "client_id": "ui.apps.example.com", "client_secret": "ui-secret-Ab12Cd"})
        # The stored row takes over from the retained copy, for both the names and the overlay.
        assert client.get("/api/system").json()["unseeded_env"] == []
        assert health._env()["GMAIL_HOME_CLIENT_ID"] == "ui.apps.example.com"
        assert connections.delete("gmail_home") is True
        assert client.get("/api/system").json()["unseeded_env"] == []
        assert "gmail_home" not in health.known_sources()

    with running_app() as client:
        assert client.get("/api/system").json()["unseeded_env"] == []
        assert "gmail_home" not in health.known_sources()


def test_a_later_successful_seed_of_the_same_id_removes_the_retention(home, monkeypatch):
    _with_key(monkeypatch)
    _set_env(home, f"GMAIL_HOME_REFRESH_TOKEN={GMAIL_REFRESH}\nGMAIL_CLIENT_ID={GMAIL_CLIENT_ID}\n")
    with running_app() as client:
        assert client.get("/api/system").json()["unseeded_env"] == ["GMAIL_CLIENT_ID", "GMAIL_HOME_REFRESH_TOKEN"]
        _set_env(home, f"GMAIL_HOME_REFRESH_TOKEN={GMAIL_REFRESH}\nGMAIL_CLIENT_ID={GMAIL_CLIENT_ID}\nGMAIL_CLIENT_SECRET={GMAIL_CLIENT_SECRET}\n")
        assert connections.seed_from_env(health._raw_env()) == ["gmail_home"]
        assert client.get("/api/system").json()["unseeded_env"] == []
        assert health._env()["GMAIL_HOME_CLIENT_SECRET"] == GMAIL_CLIENT_SECRET


def test_editing_env_for_a_stored_id_changes_nothing(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app():
        assert connections.get_secret("slack_work", "token") == SLACK_TOKEN

    _set_env(home, BAD_ALIAS_ENV.replace(SLACK_TOKEN, "xoxb-edited-in-env-Zp90Qw"))
    with running_app() as client:
        assert connections.get_secret("slack_work", "token") == SLACK_TOKEN
        assert "SLACK_WORK_TOKEN" not in client.get("/api/system").json()["unseeded_env"]
        assert "slack_work" in health.known_sources()
        assert health.slack_workspaces()["work"] == SLACK_TOKEN


def test_without_a_key_nothing_changes_and_unseeded_env_is_empty(home):
    with running_app() as client:
        assert client.get("/api/system").json()["unseeded_env"] == []
        assert connections.unseeded_env_keys() == []
        assert "m365_my_org" in health.known_sources()
        assert health.m365_tenant_config("my_org") == {"alias": "my_org", "tenant_id": TENANT, "client_id": CLIENT}
        assert client.get("/api/connections").json() == []


def test_retention_does_not_leak_into_a_keyless_second_startup(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app() as client:
        assert client.get("/api/system").json()["unseeded_env"] != []
    monkeypatch.delenv("SIGNALSLATE_SECRET_KEY")
    with running_app() as client:
        assert client.get("/api/system").json()["unseeded_env"] == []
        assert connections.overlay_provider() is None


def test_the_overlay_itself_carries_the_retained_values_but_is_read_only(home, monkeypatch):
    _with_key(monkeypatch)
    with running_app():
        overlay = connections.overlay_provider()
        assert overlay is not None
        assert overlay["M365_ORG1_TENANT_ID"] == TENANT
        assert not hasattr(overlay, "__setitem__")
