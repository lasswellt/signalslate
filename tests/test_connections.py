"""
Tests for pipeline.connections. The property under test above all others: no secret value and no
ciphertext ever leaves the module in a view, an exception message or any serialized form of either.

Every secret below is a distinctive invented string so a substring hit can only mean a leak.
"""
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, crypto, db  # noqa: E402
from pipeline.connections import (  # noqa: E402
    ConnectionError,
    ConnectionNotFound,
    DuplicateConnection,
    InvalidField,
    MissingField,
    UnknownField,
    UnknownKind,
)

ZOOM_SECRET = "zoom-secret-Qw83nLk2Pz"
SLACK_TOKEN = "slack-token-Hd91XvB7mR"
GMAIL_SECRET = "gmail-client-secret-Tj40cUe5Ya"
GMAIL_REFRESH = "gmail-refresh-Vn27sGf1Lo"
ALL_SECRETS = [ZOOM_SECRET, SLACK_TOKEN, GMAIL_SECRET, GMAIL_REFRESH]

M365 = {"alias": "Contoso-1", "tenant_id": "11111111-2222-3333-4444-555555555555", "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
ZOOM = {"account_id": "acct_Example123", "client_id": "zoomClient_Ex", "client_secret": ZOOM_SECRET}
SLACK = {"label": "work", "token": SLACK_TOKEN}
GMAIL = {
    "label": "personal",
    "client_id": "1234-example.apps.googleusercontent.com",
    "client_secret": GMAIL_SECRET,
    "refresh_token": GMAIL_REFRESH,
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


def _ciphertext(connection_id: str) -> str:
    with db.get_session() as session:
        row = session.get(db.Connection, connection_id)
        assert row is not None and row.secret_ciphertext
        return row.secret_ciphertext


def _raw_text(engine) -> str:
    """Every stored column of both tables as text, straight from SQLite."""
    parts = []
    with engine.connect() as conn:
        for table in ("connection", "tombstone"):
            for row in conn.exec_driver_sql(f"SELECT * FROM {table}"):
                parts.append(repr(tuple(row)))
    return "\n".join(parts)


def _serialized(view) -> str:
    return json.dumps(asdict(view), default=str) + repr(view) + str(view)


def _assert_no_secret(text: str, secrets=ALL_SECRETS) -> None:
    for secret in secrets:
        assert secret not in text


# --- create, per kind --------------------------------------------------------------


def test_create_m365_needs_no_secrets_and_no_vault(no_vault):
    view = connections.create("m365", M365)
    assert (view.id, view.kind, view.label, view.origin) == ("m365_Contoso-1", "m365", "Contoso-1", "ui")
    assert view.config == M365
    assert view.secrets_set == []
    assert connections.exists("m365_Contoso-1")


def test_create_zoom_is_a_singleton_with_literal_label(vault):
    view = connections.create("zoom", ZOOM)
    assert (view.id, view.label) == ("zoom", "zoom")
    assert view.config == {"account_id": "acct_Example123", "client_id": "zoomClient_Ex"}
    assert view.secrets_set == ["client_secret"]
    with pytest.raises(DuplicateConnection):
        connections.create("zoom", {**ZOOM, "client_id": "another"})


def test_create_slack(vault):
    view = connections.create("slack", SLACK)
    assert (view.id, view.label, view.config) == ("slack_work", "work", {"label": "work"})
    assert view.secrets_set == ["token"]


def test_create_gmail_defaults_redirect_mode_and_accepts_no_refresh_token(vault):
    fields = {k: v for k, v in GMAIL.items() if k != "refresh_token"}
    view = connections.create("gmail", fields)
    assert view.id == "gmail_personal"
    assert view.config["redirect_mode"] == "paste_back"
    assert view.secrets_set == ["client_secret"]


def test_create_gmail_with_refresh_token_and_callback_mode(vault):
    view = connections.create("gmail", {**GMAIL, "redirect_mode": "callback"})
    assert view.config["redirect_mode"] == "callback"
    assert view.secrets_set == ["client_secret", "refresh_token"]


def test_create_records_origin_and_creation_order(vault):
    connections.create("slack", SLACK, origin="env")
    connections.create("m365", M365)
    views = connections.list_connections()
    assert [v.id for v in views] == ["slack_work", "m365_Contoso-1"]
    assert views[0].origin == "env"
    with db.get_session() as session:
        rows = [session.get(db.Connection, v.id) for v in views]
        assert [row.seq for row in rows if row is not None] == [1, 2]


def test_create_duplicate_live_row_raises(vault):
    connections.create("slack", SLACK)
    with pytest.raises(DuplicateConnection) as info:
        connections.create("slack", {**SLACK, "token": "another-token-Zz9911"})
    assert info.value.field == "id"
    assert "another-token-Zz9911" not in str(info.value)


def test_create_over_tombstone_clears_the_tombstone(vault):
    connections.create("slack", SLACK)
    assert connections.delete("slack_work") is True
    with db.get_session() as session:
        assert session.get(db.Tombstone, "slack_work") is not None
    connections.create("slack", SLACK)
    with db.get_session() as session:
        assert session.get(db.Tombstone, "slack_work") is None
    assert connections.exists("slack_work")


# --- update ------------------------------------------------------------------------


def test_update_config_only_keeps_secrets(vault):
    connections.create("gmail", GMAIL)
    view = connections.update("gmail_personal", config={"client_id": "5678-other.apps.googleusercontent.com"})
    assert view.config["client_id"] == "5678-other.apps.googleusercontent.com"
    assert view.secrets_set == ["client_secret", "refresh_token"]
    assert connections._secrets_for("gmail_personal") == {"client_secret": GMAIL_SECRET, "refresh_token": GMAIL_REFRESH}


def test_update_replaces_only_the_named_secrets(vault):
    connections.create("gmail", GMAIL)
    connections.update("gmail_personal", secrets={"refresh_token": "gmail-refresh-NEW-Kp55"})
    assert connections._secrets_for("gmail_personal") == {
        "client_secret": GMAIL_SECRET,
        "refresh_token": "gmail-refresh-NEW-Kp55",
    }


def test_update_can_add_an_optional_secret_later(vault):
    connections.create("gmail", {k: v for k, v in GMAIL.items() if k != "refresh_token"})
    view = connections.update("gmail_personal", secrets={"refresh_token": GMAIL_REFRESH})
    assert view.secrets_set == ["client_secret", "refresh_token"]


def test_update_bumps_updated_at_and_keeps_created_at(vault):
    created = connections.create("slack", SLACK)
    updated = connections.update("slack_work", secrets={"token": "slack-token-Rotated-77"})
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at


def test_update_with_nothing_is_a_noop_that_does_not_bump_the_version(vault):
    connections.create("slack", SLACK)
    before = connections.current_version()
    view = connections.update("slack_work")
    assert view.id == "slack_work"
    assert connections.current_version() == before


def test_update_label_field_is_accepted_only_when_unchanged(vault):
    connections.create("slack", SLACK)
    assert connections.update("slack_work", config={"label": "work"}).label == "work"
    with pytest.raises(UnknownField) as info:
        connections.update("slack_work", config={"label": "other"})
    assert info.value.field == "label"
    assert connections.exists("slack_work") and not connections.exists("slack_other")


def test_update_m365_alias_cannot_change(no_vault):
    connections.create("m365", M365)
    with pytest.raises(UnknownField):
        connections.update("m365_Contoso-1", config={"alias": "Other"})
    assert connections.update("m365_Contoso-1", config={"tenant_id": "contoso.onmicrosoft.com"}).config["tenant_id"] == "contoso.onmicrosoft.com"


def test_update_unknown_id_raises_not_found(vault):
    with pytest.raises(ConnectionNotFound):
        connections.update("slack_missing", config={"label": "missing"})


def test_update_rejects_unknown_config_and_secret_names(vault):
    connections.create("slack", SLACK)
    with pytest.raises(UnknownField):
        connections.update("slack_work", config={"tenant_id": "x"})
    with pytest.raises(UnknownField):
        connections.update("slack_work", secrets={"client_secret": "wrong-kind-secret-1"})


def test_update_rejects_empty_secret_and_leaves_the_stored_one(vault):
    connections.create("slack", SLACK)
    for bad in ("", "   ", None, 12345):
        with pytest.raises(InvalidField):
            connections.update("slack_work", secrets={"token": bad})
    assert connections._secrets_for("slack_work") == {"token": SLACK_TOKEN}


def test_set_secret_stores_one_secret(vault):
    connections.create("gmail", {k: v for k, v in GMAIL.items() if k != "refresh_token"})
    view = connections.set_secret("gmail_personal", "refresh_token", GMAIL_REFRESH)
    assert view.secrets_set == ["client_secret", "refresh_token"]
    with pytest.raises(UnknownField):
        connections.set_secret("gmail_personal", "token", "not-a-gmail-secret-1")


# --- delete, get, list -------------------------------------------------------------


def test_delete_removes_row_and_writes_tombstone(vault):
    connections.create("zoom", ZOOM)
    assert connections.delete("zoom") is True
    assert not connections.exists("zoom")
    assert connections.get("zoom") is None
    with db.get_session() as session:
        assert session.get(db.Tombstone, "zoom") is not None


def test_delete_unknown_id_returns_false_and_writes_no_tombstone(vault):
    assert connections.delete("slack_never") is False
    with db.get_session() as session:
        assert session.get(db.Tombstone, "slack_never") is None


def test_delete_twice_refreshes_the_tombstone_without_error(vault):
    connections.create("slack", SLACK)
    connections.delete("slack_work")
    connections.create("slack", SLACK)
    assert connections.delete("slack_work") is True
    assert connections.delete("slack_work") is False


def test_get_and_list_return_views(vault):
    assert connections.get("zoom") is None and connections.list_connections() == []
    connections.create("zoom", ZOOM)
    view = connections.get("zoom")
    assert view is not None and view.kind == "zoom"
    assert [v.id for v in connections.list_connections()] == ["zoom"]


# --- validation matrix -------------------------------------------------------------


@pytest.mark.parametrize("alias", ["../x", "a/b", "a b", "a\\b", "..", "a.b", "", "x" * 41, "a\nb", "Contoso\n", "café", "١٢"])
def test_m365_alias_rejects_traversal_and_bad_characters(no_vault, alias):
    with pytest.raises(InvalidField) as info:
        connections.create("m365", {**M365, "alias": alias})
    assert info.value.field == "alias"
    assert connections.list_connections() == []


def test_m365_alias_accepts_the_boundary_and_mixed_case(no_vault):
    assert connections.create("m365", {**M365, "alias": "A" * 40}).id == "m365_" + "A" * 40
    assert connections.create("m365", {**M365, "alias": "Mixed-Case-9"}).label == "Mixed-Case-9"


@pytest.mark.parametrize("kind,extra", [("slack", {"token": SLACK_TOKEN}), ("gmail", {"client_id": "1234-x.apps.googleusercontent.com", "client_secret": GMAIL_SECRET})])
@pytest.mark.parametrize("label", ["Work", "WORK", "wo-rk", "wo_rk", "wo rk", "a/b", "../x", "", "x" * 33, "work\n", "w.k"])
def test_slack_and_gmail_labels_reject_uppercase_and_symbols(vault, kind, extra, label):
    with pytest.raises(InvalidField) as info:
        connections.create(kind, {"label": label, **extra})
    assert info.value.field == "label"


@pytest.mark.parametrize("kind,extra", [("slack", {"token": SLACK_TOKEN}), ("gmail", {"client_id": "1234-x.apps.googleusercontent.com", "client_secret": GMAIL_SECRET})])
def test_slack_and_gmail_label_length_boundary(vault, kind, extra):
    assert connections.create(kind, {"label": "a" * 32, **extra}).label == "a" * 32


@pytest.mark.parametrize("field", ["tenant_id", "client_id"])
@pytest.mark.parametrize("value", ["", "has space", "a/b", "x" * 129, "id\n", "id;drop", None, 7, ["a"]])
def test_m365_identifiers_are_validated(no_vault, field, value):
    with pytest.raises(InvalidField) as info:
        connections.create("m365", {**M365, field: value})
    assert info.value.field == field


@pytest.mark.parametrize("mode", ["", "PASTE_BACK", "popup", None, 1])
def test_gmail_redirect_mode_is_an_enum(vault, mode):
    with pytest.raises(InvalidField) as info:
        connections.create("gmail", {**GMAIL, "redirect_mode": mode})
    assert info.value.field == "redirect_mode"


@pytest.mark.parametrize("bad", ["", "   ", None, 5, ["x"], {"a": 1}, "x" * 4097, "tok\nen-with-newline", "tok\x00en-with-nul"])
def test_secret_values_must_be_clean_non_empty_strings(vault, bad):
    with pytest.raises(InvalidField) as info:
        connections.create("slack", {"label": "work", "token": bad})
    assert info.value.field == "token"
    assert connections.list_connections() == []


def test_missing_required_fields_are_named(vault):
    cases = [
        ("m365", {"alias": "a", "tenant_id": "t"}, "client_id"),
        ("zoom", {"account_id": "a", "client_id": "c"}, "client_secret"),
        ("zoom", {"client_id": "c", "client_secret": ZOOM_SECRET}, "account_id"),
        ("slack", {"label": "work"}, "token"),
        ("slack", {"token": SLACK_TOKEN}, "label"),
        ("gmail", {"label": "p", "client_id": "c"}, "client_secret"),
        ("gmail", {"label": "p", "client_secret": GMAIL_SECRET}, "client_id"),
    ]
    for kind, fields, missing in cases:
        with pytest.raises(MissingField) as info:
            connections.create(kind, fields)
        assert info.value.field == missing
    assert connections.list_connections() == []


@pytest.mark.parametrize("kind", ["", "Slack", "teams", None, 3, "slack "])
def test_unknown_kind_is_rejected_without_echo(vault, kind):
    with pytest.raises(UnknownKind) as info:
        connections.create(kind, SLACK)
    assert info.value.field == "kind"
    if isinstance(kind, str) and kind:
        assert kind not in str(info.value)


def test_unknown_fields_are_rejected(vault):
    with pytest.raises(UnknownField) as info:
        connections.create("slack", {**SLACK, "extra": "x"})
    assert info.value.field == "extra"
    with pytest.raises(UnknownField):
        connections.create("m365", {**M365, "client_secret": "nope-nope-nope-1"})
    with pytest.raises(UnknownField):
        connections.create("zoom", {**ZOOM, "label": "zoom"})


def test_a_secret_pasted_as_a_field_name_is_not_echoed(vault):
    pasted = "xoxb-1111-2222-Abcdefghijkl"
    with pytest.raises(UnknownField) as info:
        connections.create("slack", {**SLACK, pasted: "x"})
    assert pasted not in str(info.value) and pasted not in info.value.field
    connections.create("slack", SLACK)
    with pytest.raises(UnknownField) as info:
        connections.update("slack_work", secrets={pasted: "x"})
    assert pasted not in str(info.value) and pasted not in info.value.field


def test_non_mapping_fields_and_bad_origin_are_rejected(vault):
    not_a_mapping: Any = ["label", "token"]
    with pytest.raises(InvalidField):
        connections.create("slack", not_a_mapping)
    with pytest.raises(InvalidField):
        connections.create("slack", SLACK, origin="import")


# --- vault handling ----------------------------------------------------------------


def test_create_with_secrets_and_no_vault_raises_and_persists_nothing(no_vault):
    for kind, fields in (("zoom", ZOOM), ("slack", SLACK), ("gmail", GMAIL)):
        with pytest.raises(crypto.SecretKeyMissing) as info:
            connections.create(kind, fields)
        _assert_no_secret(str(info.value))
    assert connections.list_connections() == []


def test_update_with_secrets_and_no_vault_raises_and_changes_nothing(vault):
    connections.create("slack", SLACK)
    connections.set_vault(None)
    with pytest.raises(crypto.SecretKeyMissing) as info:
        connections.update("slack_work", secrets={"token": "slack-token-Later-4412"})
    assert "slack-token-Later-4412" not in str(info.value)
    connections.set_vault(vault)
    assert connections._secrets_for("slack_work") == {"token": SLACK_TOKEN}


def test_config_only_update_works_without_a_vault_and_keeps_the_ciphertext(vault):
    connections.create("gmail", GMAIL)
    before = _ciphertext("gmail_personal")
    connections.set_vault(None)
    connections.update("gmail_personal", config={"redirect_mode": "callback"})
    assert _ciphertext("gmail_personal") == before


def test_views_stay_readable_without_a_vault_or_with_the_wrong_key(vault):
    connections.create("slack", SLACK)
    connections.set_vault(None)
    view = connections.get("slack_work")
    assert view is not None and view.secrets_set == []
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    assert connections.list_connections()[0].secrets_set == []
    with pytest.raises(crypto.SecretDecryptError) as info:
        connections._secrets_for("slack_work")
    assert SLACK_TOKEN not in str(info.value)
    with pytest.raises(crypto.SecretDecryptError):
        connections.update("slack_work", secrets={"token": "slack-token-Newer-9090"})


def test_rotated_vault_still_opens_old_secrets(vault):
    connections.create("slack", SLACK)
    old_key = crypto.generate_key()
    connections.set_vault(crypto.Vault([old_key]))
    connections.create("zoom", ZOOM)
    connections.set_vault(crypto.Vault([crypto.generate_key(), old_key]))
    assert connections._secrets_for("zoom") == {"client_secret": ZOOM_SECRET}


def test_set_vault_get_vault_roundtrip(temp_db):
    v = crypto.Vault([crypto.generate_key()])
    connections.set_vault(v)
    assert connections.get_vault() is v
    connections.set_vault(None)
    assert connections.get_vault() is None


# --- write-only property -----------------------------------------------------------


def _create_all():
    connections.create("m365", M365)
    connections.create("zoom", ZOOM)
    connections.create("slack", SLACK)
    connections.create("gmail", GMAIL)


def test_no_view_contains_a_secret_or_a_ciphertext(vault, temp_db):
    _create_all()
    connections.update("slack_work", secrets={"token": "slack-token-Rotated-3131"})
    connections.set_secret("gmail_personal", "refresh_token", "gmail-refresh-Set-8080")
    secrets = ALL_SECRETS + ["slack-token-Rotated-3131", "gmail-refresh-Set-8080"]
    ciphertexts = [_ciphertext(v.id) for v in connections.list_connections() if v.secrets_set]
    assert len(ciphertexts) == 3

    views = connections.list_connections() + [connections.get(cid) for cid in ("zoom", "slack_work", "gmail_personal", "m365_Contoso-1")]
    for view in views:
        assert view is not None
        text = _serialized(view)
        _assert_no_secret(text, secrets)
        for token in ciphertexts:
            assert token not in text
        assert "ciphertext" not in text.lower()
        assert set(asdict(view)) == {"id", "kind", "label", "origin", "config", "secrets_set", "created_at", "updated_at"}


def test_view_config_never_holds_a_secret_name_or_value(vault):
    _create_all()
    for view in connections.list_connections():
        assert not set(view.config) & {"client_secret", "refresh_token", "token"}


def test_views_returned_from_writes_are_clean_too(vault):
    views = [
        connections.create("zoom", ZOOM),
        connections.create("gmail", GMAIL),
        connections.update("zoom", secrets={"client_secret": "zoom-secret-Fresh-6060"}),
        connections.set_secret("gmail_personal", "refresh_token", "gmail-refresh-Fresh-7070"),
    ]
    for view in views:
        _assert_no_secret(_serialized(view), ALL_SECRETS + ["zoom-secret-Fresh-6060", "gmail-refresh-Fresh-7070"])


def test_secrets_set_lists_sorted_names_only(vault):
    view = connections.create("gmail", {**GMAIL})
    assert view.secrets_set == sorted(view.secrets_set) == ["client_secret", "refresh_token"]


def test_storage_holds_no_plaintext_secret(vault, temp_db):
    _create_all()
    connections.update("zoom", secrets={"client_secret": "zoom-secret-Fresh-6060"})
    connections.delete("slack_work")
    raw = _raw_text(temp_db)
    _assert_no_secret(raw, ALL_SECRETS + ["zoom-secret-Fresh-6060"])
    assert "secret_ciphertext" not in raw  # sanity: values, not column names, were dumped


def test_secrets_for_returns_plaintext_only_through_the_private_door(vault):
    _create_all()
    assert connections._secrets_for("slack_work") == {"token": SLACK_TOKEN}
    assert connections._secrets_for("m365_Contoso-1") == {}
    assert not hasattr(connections, "secrets_for")
    with pytest.raises(ConnectionNotFound):
        connections._secrets_for("slack_missing")


def test_error_messages_never_contain_a_submitted_secret(vault):
    leaked = "leaky-secret-Zx0912Ab"
    attempts = [
        lambda: connections.create("slack", {"label": "Bad Label", "token": leaked}),
        lambda: connections.create("slack", {"label": "ok", "token": leaked, "unknown": leaked}),
        lambda: connections.create("m365", {**M365, "client_secret": leaked}),
        lambda: connections.create("gmail", {**GMAIL, "client_secret": leaked, "redirect_mode": leaked}),
        lambda: connections.create("gmail", {**GMAIL, "client_secret": leaked + "\n"}),
        lambda: connections.create("gmail", {"label": "p", "client_secret": leaked}),
        lambda: connections.create("zoom", {**ZOOM, "client_secret": leaked, "client_id": leaked + " space"}),
        lambda: connections.create(leaked, {"label": "x", "token": leaked}),
        lambda: connections.create("slack", {"label": "ok", "token": leaked, "extra": 1}),
    ]
    for attempt in attempts:
        with pytest.raises(ConnectionError) as info:
            attempt()
        text = str(info.value) + repr(info.value) + info.value.field + info.value.message
        assert leaked not in text

    connections.create("slack", {"label": "ok", "token": leaked})
    with pytest.raises(DuplicateConnection) as info:
        connections.create("slack", {"label": "ok", "token": leaked})
    assert leaked not in str(info.value) + repr(info.value)
    with pytest.raises(UnknownField) as info:
        connections.update("slack_ok", config={"label": "renamed"}, secrets={"token": leaked})
    assert leaked not in str(info.value) + repr(info.value)
    with pytest.raises(InvalidField) as info:
        connections.update("slack_ok", secrets={"token": leaked + "\x00"})
    assert leaked not in str(info.value) + repr(info.value)


def test_error_carries_field_name(vault):
    with pytest.raises(ConnectionError) as info:
        connections.create("slack", {"label": "UP", "token": SLACK_TOKEN})
    assert info.value.field == "label"
    assert str(info.value).startswith("label: ")


def test_a_failed_write_after_validation_leaves_no_secret_in_the_exception_chain(vault):
    """The vault lookup happens after validation; a decrypt failure must not chain the ciphertext."""
    connections.create("slack", SLACK)
    ciphertext = _ciphertext("slack_work")
    connections.set_vault(crypto.Vault([crypto.generate_key()]))
    with pytest.raises(crypto.SecretDecryptError) as info:
        connections.update("slack_work", secrets={"token": "slack-token-Other-2020"})
    chain = repr(info.value) + repr(info.value.__cause__) + repr(info.value.__context__)
    assert ciphertext not in chain and SLACK_TOKEN not in chain and "slack-token-Other-2020" not in chain


# --- version counter, imports ------------------------------------------------------


def test_every_write_bumps_the_version_and_reads_do_not(vault):
    start = connections.current_version()
    connections.create("slack", SLACK)
    assert connections.current_version() == start + 1
    connections.get("slack_work")
    connections.list_connections()
    connections.exists("slack_work")
    assert connections.current_version() == start + 1
    connections.update("slack_work", secrets={"token": "slack-token-Ver-1111"})
    connections.set_secret("slack_work", "token", "slack-token-Ver-2222")
    connections.delete("slack_work")
    assert connections.current_version() == start + 4


def test_failed_writes_do_not_bump_the_version(vault):
    connections.create("slack", SLACK)
    before = connections.current_version()
    with pytest.raises(DuplicateConnection):
        connections.create("slack", SLACK)
    with pytest.raises(InvalidField):
        connections.create("slack", {"label": "UP", "token": SLACK_TOKEN})
    with pytest.raises(ConnectionNotFound):
        connections.update("slack_missing", config={"label": "missing"})
    assert connections.delete("slack_missing") is False
    assert connections.current_version() == before


def test_module_imports_neither_config_store_nor_health():
    code = (
        "import sys; import pipeline.connections; "
        "bad = [m for m in ('pipeline.config_store', 'pipeline.health') if m in sys.modules]; "
        "sys.exit(1 if bad else 0)"
    )
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
