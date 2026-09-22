"""
Tests for the env seeding, the env-style materialization and the overlay provider in
pipeline.connections. The acceptance test is the round trip: the overlay, fed to the EXISTING
pipeline.health parsers, must give back exactly what .env declared.

Every secret below is a distinctive invented string so a substring hit can only mean a leak.
"""
import logging
import sys
from pathlib import Path
from typing import Mapping, MutableMapping, cast

import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, crypto, db, health  # noqa: E402
from pipeline.redact import redact  # noqa: E402

ZOOM_SECRET = "zoom-secret-Qw83nLk2Pz"
SLACK_TOKEN = "slack-token-Hd91XvB7mR"
OTHER_SLACK_TOKEN = "slack-token-Bb17WqC4dS"
GMAIL_SECRET = "gmail-client-secret-Tj40cUe5Ya"
GMAIL_REFRESH = "gmail-refresh-Vn27sGf1Lo"
PERSONAL_SECRET = "gmail-personal-secret-Ka55oPd9Zx"
ALL_SECRETS = [ZOOM_SECRET, SLACK_TOKEN, OTHER_SLACK_TOKEN, GMAIL_SECRET, GMAIL_REFRESH, PERSONAL_SECRET]

TENANT_1 = "11111111-2222-3333-4444-555555555555"
TENANT_2 = "66666666-7777-8888-9999-000000000000"
SHARED_CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ORG_CLIENT = "ffffffff-0000-1111-2222-333333333333"
GMAIL_SHARED_ID = "1234-shared.apps.googleusercontent.com"
GMAIL_PERSONAL_ID = "5678-personal.apps.googleusercontent.com"

RAW_ENV = {
    "M365_CLIENT_ID": SHARED_CLIENT,
    "M365_ORG1_ALIAS": "Contoso",
    "M365_ORG1_TENANT_ID": TENANT_1,
    "M365_ORG2_ALIAS": "Fabrikam",
    "M365_ORG2_TENANT_ID": TENANT_2,
    "M365_ORG2_CLIENT_ID": ORG_CLIENT,
    "ZOOM_ACCOUNT_ID": "acct_Example123",
    "ZOOM_CLIENT_ID": "zoomClient_Ex",
    "ZOOM_CLIENT_SECRET": ZOOM_SECRET,
    "SLACK_WORK_TOKEN": SLACK_TOKEN,
    "SLACK_SKIP_DMS": "true",
    "GMAIL_CLIENT_ID": GMAIL_SHARED_ID,
    "GMAIL_CLIENT_SECRET": GMAIL_SECRET,
    "GMAIL_HOME_REFRESH_TOKEN": GMAIL_REFRESH,
    "GMAIL_PERSONAL_REFRESH_TOKEN": "gmail-refresh-Personal77",
    "GMAIL_PERSONAL_CLIENT_ID": GMAIL_PERSONAL_ID,
    "GMAIL_PERSONAL_CLIENT_SECRET": PERSONAL_SECRET,
    "TZ": "UTC",
    "ANTHROPIC_API_KEY": "sk-ant-not-a-connection",
}


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def vault(temp_db):
    v = crypto.Vault([crypto.generate_key()])
    connections.set_vault(v)
    yield v
    connections.set_vault(None)


@pytest.fixture
def no_vault(temp_db):
    connections.set_vault(None)
    yield
    connections.set_vault(None)


def _row_origin(connection_id: str) -> str:
    with db.get_session() as session:
        row = session.get(db.Connection, connection_id)
        assert row is not None
        return row.origin


def _view(connection_id: str) -> connections.ConnectionView:
    view = connections.get(connection_id)
    assert view is not None
    return view


def _overlay() -> Mapping[str, str]:
    overlay = connections.overlay_provider()
    assert overlay is not None
    return overlay


def _ciphertext(connection_id: str) -> str:
    with db.get_session() as session:
        row = session.get(db.Connection, connection_id)
        assert row is not None and row.secret_ciphertext
        return row.secret_ciphertext


# --- seed_from_env -----------------------------------------------------------------


def test_seed_creates_every_kind_with_origin_env(vault):
    seeded = connections.seed_from_env(RAW_ENV)
    assert sorted(seeded) == [
        "gmail_home",
        "gmail_personal",
        "m365_Contoso",
        "m365_Fabrikam",
        "slack_work",
        "zoom",
    ]
    assert all(_row_origin(connection_id) == "env" for connection_id in seeded)
    assert _view("zoom").config == {"account_id": "acct_Example123", "client_id": "zoomClient_Ex"}
    assert connections.get_secret("zoom", "client_secret") == ZOOM_SECRET
    assert connections.get_secret("slack_work", "token") == SLACK_TOKEN
    assert connections.get_secret("gmail_home", "refresh_token") == GMAIL_REFRESH


def test_seed_m365_uses_shared_client_id_unless_the_org_has_its_own(vault):
    connections.seed_from_env(RAW_ENV)
    assert _view("m365_Contoso").config["client_id"] == SHARED_CLIENT
    assert _view("m365_Fabrikam").config["client_id"] == ORG_CLIENT
    assert _view("m365_Contoso").config["tenant_id"] == TENANT_1


def test_seed_gmail_uses_shared_credentials_unless_the_label_has_its_own(vault):
    connections.seed_from_env(RAW_ENV)
    assert _view("gmail_home").config["client_id"] == GMAIL_SHARED_ID
    assert connections.get_secret("gmail_home", "client_secret") == GMAIL_SECRET
    assert _view("gmail_personal").config["client_id"] == GMAIL_PERSONAL_ID
    assert connections.get_secret("gmail_personal", "client_secret") == PERSONAL_SECRET


def test_seed_lowercases_labels_and_ignores_non_connection_keys(vault):
    seeded = connections.seed_from_env({"SLACK_WORK_TOKEN": SLACK_TOKEN, "SLACK_SKIP_DMS": "true", "TZ": "UTC"})
    assert seeded == ["slack_work"]
    assert _view("slack_work").label == "work"


def test_seed_is_idempotent(vault):
    first = connections.seed_from_env(RAW_ENV)
    version = connections.current_version()
    assert first
    assert connections.seed_from_env(RAW_ENV) == []
    assert connections.current_version() == version
    assert len(connections.list_connections()) == len(first)


def test_seed_does_not_bring_back_a_tombstoned_id(vault):
    connections.seed_from_env(RAW_ENV)
    assert connections.delete("slack_work")
    assert connections.delete("m365_Contoso")
    assert connections.seed_from_env(RAW_ENV) == []
    assert not connections.exists("slack_work")
    assert not connections.exists("m365_Contoso")


def test_an_explicit_create_after_delete_clears_the_tombstone_so_seeding_sees_a_live_row(vault):
    connections.seed_from_env({"SLACK_WORK_TOKEN": SLACK_TOKEN})
    connections.delete("slack_work")
    connections.create("slack", {"label": "work", "token": OTHER_SLACK_TOKEN})
    assert connections.seed_from_env({"SLACK_WORK_TOKEN": SLACK_TOKEN}) == []
    assert connections.get_secret("slack_work", "token") == OTHER_SLACK_TOKEN


def test_seed_never_overwrites_an_existing_row_store_wins(vault):
    connections.create("slack", {"label": "work", "token": OTHER_SLACK_TOKEN})
    connections.create("m365", {"alias": "Contoso", "tenant_id": TENANT_2, "client_id": ORG_CLIENT})
    assert connections.seed_from_env(RAW_ENV).count("slack_work") == 0
    assert connections.get_secret("slack_work", "token") == OTHER_SLACK_TOKEN
    assert _row_origin("slack_work") == "ui"
    assert _view("m365_Contoso").config["tenant_id"] == TENANT_2


def test_seed_skips_a_malformed_declaration_reports_it_and_keeps_going(vault, caplog):
    raw = {
        "M365_ORG1_ALIAS": "NoTenant",  # no tenant id
        "M365_ORG2_ALIAS": "Fabrikam",
        "M365_ORG2_TENANT_ID": TENANT_2,
        "M365_ORG2_CLIENT_ID": ORG_CLIENT,
        "M365_ORG3_ALIAS": "Bad.Alias",  # a dot cannot be in a token cache file name
        "M365_ORG3_TENANT_ID": TENANT_1,
        "M365_ORG3_CLIENT_ID": ORG_CLIENT,
        "GMAIL_NOSECRET_REFRESH_TOKEN": GMAIL_REFRESH,  # no client id or secret anywhere
        "SLACK_" + "A" * 40 + "_TOKEN": SLACK_TOKEN,  # label longer than the limit
        "SLACK_GOOD_TOKEN": OTHER_SLACK_TOKEN,
    }
    with caplog.at_level(logging.WARNING, logger="pipeline.connections"):
        seeded = connections.seed_from_env(raw)
    assert sorted(seeded) == ["m365_Fabrikam", "slack_good"]
    assert "M365_ORG1_ALIAS" in caplog.text
    assert "GMAIL_NOSECRET_REFRESH_TOKEN" in caplog.text
    for secret in ALL_SECRETS:
        assert secret not in caplog.text


def test_seed_skips_a_partial_zoom_declaration(vault, caplog):
    with caplog.at_level(logging.WARNING, logger="pipeline.connections"):
        seeded = connections.seed_from_env({"ZOOM_ACCOUNT_ID": "acct_Example123", "ZOOM_CLIENT_SECRET": ZOOM_SECRET})
    assert seeded == []
    assert not connections.exists("zoom")
    assert ZOOM_SECRET not in caplog.text


def test_seed_treats_blank_placeholders_as_undeclared(vault, caplog):
    placeholders = {
        "M365_CLIENT_ID": "",
        "M365_ORG2_ALIAS": "",
        "M365_ORG2_TENANT_ID": "",
        "ZOOM_ACCOUNT_ID": "",
        "ZOOM_CLIENT_ID": "",
        "ZOOM_CLIENT_SECRET": "",
        "SLACK_WORK_TOKEN": "",
        "GMAIL_PERSONAL_REFRESH_TOKEN": None,  # dotenv gives None for a key with no "="
    }
    with caplog.at_level(logging.WARNING, logger="pipeline.connections"):
        assert connections.seed_from_env(placeholders) == []
    assert caplog.text == ""


def test_seed_without_a_vault_seeds_nothing(no_vault):
    assert connections.seed_from_env(RAW_ENV) == []
    assert connections.list_connections() == []


def test_seed_gmail_account_without_secrets_is_never_created_from_partial_env(vault):
    assert connections.seed_from_env({"GMAIL_HOME_REFRESH_TOKEN": GMAIL_REFRESH, "GMAIL_CLIENT_ID": GMAIL_SHARED_ID}) == []
    assert connections.list_connections() == []


# --- materialize -------------------------------------------------------------------


def test_materialize_slack_gmail_zoom_keys(vault):
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    connections.create(
        "gmail",
        {"label": "home", "client_id": GMAIL_SHARED_ID, "client_secret": GMAIL_SECRET, "refresh_token": GMAIL_REFRESH},
    )
    connections.create(
        "zoom", {"account_id": "acct_Example123", "client_id": "zoomClient_Ex", "client_secret": ZOOM_SECRET}
    )
    assert connections.materialize() == {
        "SLACK_WORK_TOKEN": SLACK_TOKEN,
        "GMAIL_HOME_REFRESH_TOKEN": GMAIL_REFRESH,
        "GMAIL_HOME_CLIENT_ID": GMAIL_SHARED_ID,
        "GMAIL_HOME_CLIENT_SECRET": GMAIL_SECRET,
        "ZOOM_ACCOUNT_ID": "acct_Example123",
        "ZOOM_CLIENT_ID": "zoomClient_Ex",
        "ZOOM_CLIENT_SECRET": ZOOM_SECRET,
    }


def test_materialize_gmail_without_a_refresh_token_omits_only_that_key(vault):
    connections.create("gmail", {"label": "home", "client_id": GMAIL_SHARED_ID, "client_secret": GMAIL_SECRET})
    overlay = connections.materialize()
    assert "GMAIL_HOME_REFRESH_TOKEN" not in overlay
    assert overlay["GMAIL_HOME_CLIENT_ID"] == GMAIL_SHARED_ID
    assert overlay["GMAIL_HOME_CLIENT_SECRET"] == GMAIL_SECRET


def test_materialize_m365_needs_no_vault_and_numbers_from_one(no_vault):
    connections.create("m365", {"alias": "Contoso", "tenant_id": TENANT_1, "client_id": SHARED_CLIENT})
    connections.create("m365", {"alias": "Fabrikam", "tenant_id": TENANT_2, "client_id": ORG_CLIENT})
    assert connections.materialize() == {
        "M365_ORG1_ALIAS": "Contoso",
        "M365_ORG1_TENANT_ID": TENANT_1,
        "M365_ORG1_CLIENT_ID": SHARED_CLIENT,
        "M365_ORG2_ALIAS": "Fabrikam",
        "M365_ORG2_TENANT_ID": TENANT_2,
        "M365_ORG2_CLIENT_ID": ORG_CLIENT,
    }


def _m365(alias: str, tenant: str) -> None:
    connections.create("m365", {"alias": alias, "tenant_id": tenant, "client_id": SHARED_CLIENT})


def _aliases_by_number(overlay: dict[str, str]) -> dict[int, str]:
    return {int(key[len("M365_ORG") : -len("_ALIAS")]): value for key, value in overlay.items() if key.endswith("_ALIAS")}


def test_materialize_m365_numbering_is_dense_and_a_re_added_org_goes_last(vault):
    _m365("Alpha", TENANT_1)
    _m365("Bravo", TENANT_2)
    _m365("Charlie", TENANT_1)
    assert _aliases_by_number(connections.materialize()) == {1: "Alpha", 2: "Bravo", 3: "Charlie"}

    connections.delete("m365_Bravo")
    after_delete = connections.materialize()
    assert _aliases_by_number(after_delete) == {1: "Alpha", 2: "Charlie"}
    assert "M365_ORG3_ALIAS" not in after_delete

    _m365("Bravo", TENANT_2)
    after_readd = connections.materialize()
    assert _aliases_by_number(after_readd) == {1: "Alpha", 2: "Charlie", 3: "Bravo"}
    assert after_readd["M365_ORG3_TENANT_ID"] == TENANT_2
    assert after_readd["M365_ORG2_TENANT_ID"] == TENANT_1


def test_materialize_leaves_out_a_row_it_cannot_decrypt_and_keeps_the_rest(vault, caplog):
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    _m365("Alpha", TENANT_1)
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    with caplog.at_level(logging.WARNING, logger="pipeline.connections"):
        overlay = connections.materialize()
    assert overlay == {"M365_ORG1_ALIAS": "Alpha", "M365_ORG1_TENANT_ID": TENANT_1, "M365_ORG1_CLIENT_ID": SHARED_CLIENT}
    assert "slack_work" in caplog.text
    assert SLACK_TOKEN not in caplog.text


def test_materialized_keys_are_all_family_keys(vault):
    connections.seed_from_env(RAW_ENV)
    overlay = connections.materialize()
    assert overlay
    assert all(connections.is_family_key(key) for key in overlay)


# --- is_family_key -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "M365_ORG1_ALIAS",
        "M365_ORG12_TENANT_ID",
        "M365_ORG3_CLIENT_ID",
        "M365_CLIENT_ID",
        "SLACK_WORK_TOKEN",
        "SLACK_TEAM2_TOKEN",
        "ZOOM_ACCOUNT_ID",
        "ZOOM_CLIENT_ID",
        "ZOOM_CLIENT_SECRET",
        "GMAIL_CLIENT_ID",
        "GMAIL_CLIENT_SECRET",
        "GMAIL_PERSONAL_REFRESH_TOKEN",
        "GMAIL_PERSONAL_CLIENT_ID",
        "GMAIL_PERSONAL_CLIENT_SECRET",
    ],
)
def test_family_keys(name):
    assert connections.is_family_key(name)
    assert any(pattern.fullmatch(name) for pattern in connections.FAMILY_KEY_PATTERNS)


@pytest.mark.parametrize(
    "name",
    [
        "SLACK_SKIP_DMS",
        "TZ",
        "ANTHROPIC_API_KEY",
        "SIGNALSLATE_MAP_MODEL",
        "SIGNALSLATE_SECRET_KEY",
        "RMAPI_CONFIG",
        "LAN_HOST",
        "M365_ORG_ALIAS",
        "M365_TENANT",
        "SLACK_WORK_TOKEN_EXTRA",
        "SLACK_work_TOKEN",
        "ZOOM_WEBHOOK_SECRET",
        "GMAIL_PERSONAL",
        "MSTODO_CLIENT_ID",
        "",
    ],
)
def test_non_family_keys(name):
    assert not connections.is_family_key(name)


# --- overlay_provider --------------------------------------------------------------


def test_overlay_provider_is_none_without_a_vault(no_vault):
    _m365("Alpha", TENANT_1)
    assert connections.overlay_provider() is None


def test_overlay_provider_caches_until_a_write_bumps_the_version(vault):
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    first = _overlay()
    assert first["SLACK_WORK_TOKEN"] == SLACK_TOKEN
    assert _overlay() is first

    connections.set_secret("slack_work", "token", OTHER_SLACK_TOKEN)
    second = _overlay()
    assert second is not first
    assert second["SLACK_WORK_TOKEN"] == OTHER_SLACK_TOKEN
    assert first["SLACK_WORK_TOKEN"] == SLACK_TOKEN  # the old snapshot is untouched
    assert _overlay() is second

    connections.delete("slack_work")
    assert "SLACK_WORK_TOKEN" not in _overlay()


def test_overlay_provider_result_is_read_only(vault):
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    overlay = _overlay()
    writable = cast(MutableMapping[str, str], overlay)
    with pytest.raises(TypeError):
        writable["SLACK_WORK_TOKEN"] = "x"
    assert dict(overlay)["SLACK_WORK_TOKEN"] == SLACK_TOKEN


def test_overlay_provider_rebuilds_when_the_vault_changes_without_a_write(vault):
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    assert _overlay()["SLACK_WORK_TOKEN"] == SLACK_TOKEN
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    assert "SLACK_WORK_TOKEN" not in _overlay()


# --- round trip through the existing health parsers ---------------------------------


def test_round_trip_through_the_existing_health_functions(vault, monkeypatch):
    connections.seed_from_env(RAW_ENV)
    overlay = dict(connections.materialize())
    monkeypatch.setattr(health, "_env", lambda: overlay)

    assert health.m365_aliases() == ["Contoso", "Fabrikam"]
    assert health.m365_tenant_config("Contoso") == {"alias": "Contoso", "tenant_id": TENANT_1, "client_id": SHARED_CLIENT}
    assert health.m365_tenant_config("Fabrikam") == {"alias": "Fabrikam", "tenant_id": TENANT_2, "client_id": ORG_CLIENT}
    assert health.m365_tenant_config("Unknown") is None
    assert health.slack_workspaces() == {"work": SLACK_TOKEN}
    assert health.gmail_accounts() == {"home": GMAIL_REFRESH, "personal": "gmail-refresh-Personal77"}
    assert overlay["ZOOM_ACCOUNT_ID"] == RAW_ENV["ZOOM_ACCOUNT_ID"]
    assert overlay["ZOOM_CLIENT_ID"] == RAW_ENV["ZOOM_CLIENT_ID"]
    assert overlay["ZOOM_CLIENT_SECRET"] == ZOOM_SECRET


def test_round_trip_after_a_delete_and_re_add_still_resolves_every_alias(vault, monkeypatch):
    connections.seed_from_env(RAW_ENV)
    connections.delete("m365_Contoso")
    _m365("Contoso", TENANT_2)
    overlay = dict(connections.materialize())
    monkeypatch.setattr(health, "_env", lambda: overlay)
    assert health.m365_aliases() == ["Fabrikam", "Contoso"]
    assert health.m365_tenant_config("Contoso") == {"alias": "Contoso", "tenant_id": TENANT_2, "client_id": SHARED_CLIENT}
    fabrikam = health.m365_tenant_config("Fabrikam")
    assert fabrikam is not None and fabrikam["tenant_id"] == TENANT_2


# --- secret_values -----------------------------------------------------------------


def test_secret_values_lists_every_secret_and_feeds_redact(vault):
    connections.seed_from_env(RAW_ENV)
    values = connections.secret_values()
    assert {SLACK_TOKEN, ZOOM_SECRET, GMAIL_SECRET, GMAIL_REFRESH, PERSONAL_SECRET} <= set(values)
    assert all(values)
    scrubbed = redact(f"failed with {SLACK_TOKEN} and {GMAIL_REFRESH}", values)
    assert SLACK_TOKEN not in scrubbed and GMAIL_REFRESH not in scrubbed


def test_secret_values_excludes_empty_values_and_rows_without_secrets(vault):
    _m365("Alpha", TENANT_1)
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    with db.get_session() as session:
        row = session.get(db.Connection, "slack_work")
        assert row is not None
        row.secret_ciphertext = vault.encrypt_json({"token": SLACK_TOKEN, "spare": "", "other": None})
        session.add(row)
        session.commit()
    assert connections.secret_values() == [SLACK_TOKEN]


def test_secret_values_skips_rows_it_cannot_decrypt(vault):
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    assert connections.secret_values() == []
    connections.set_vault(None)
    assert connections.secret_values() == []


# --- get_secret --------------------------------------------------------------------


def test_get_secret_returns_the_stored_value(vault):
    connections.create("zoom", {"account_id": "acct_Example123", "client_id": "zoomClient_Ex", "client_secret": ZOOM_SECRET})
    assert connections.get_secret("zoom", "client_secret") == ZOOM_SECRET


def test_get_secret_is_none_for_a_missing_name_or_id(vault):
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    assert connections.get_secret("slack_work", "client_secret") is None
    assert connections.get_secret("slack_nope", "token") is None
    _m365("Alpha", TENANT_1)
    assert connections.get_secret("m365_Alpha", "token") is None


def test_get_secret_does_not_echo_ciphertext_when_decryption_fails(vault):
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    ciphertext = _ciphertext("slack_work")
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    with pytest.raises(crypto.SecretDecryptError) as failure:
        connections.get_secret("slack_work", "token")
    assert ciphertext not in str(failure.value) and SLACK_TOKEN not in str(failure.value)
    assert failure.value.__cause__ is None


def test_get_secret_without_a_vault_raises_a_generic_error(vault):
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    ciphertext = _ciphertext("slack_work")
    connections.set_vault(None)
    with pytest.raises(crypto.SecretKeyMissing) as failure:
        connections.get_secret("slack_work", "token")
    assert ciphertext not in str(failure.value) and SLACK_TOKEN not in str(failure.value)
