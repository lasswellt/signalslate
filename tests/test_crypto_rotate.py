"""
Tests for key rotation: connections.rotate_all and `python -m pipeline.crypto rotate`.

The store is redirected to a temporary SQLite file for every test (db.DB_PATH is hard-coded and
DATABASE_URL is ignored), and the CLI is only ever run in-process: a subprocess would open the real
data/digest.db. The property under test above all others: rotation neither loses a secret nor prints
one, so every CLI test checks stdout and stderr for the secrets, the keys and the ciphertexts.

Every secret below is a distinctive invented string so a substring hit can only mean a leak.
"""
import json
import runpy
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, crypto, db, health  # noqa: E402
from pipeline.connections import RotationResult  # noqa: E402
from pipeline.crypto import SecretKeyMissing, Vault  # noqa: E402

ZOOM_SECRET = "zoom-secret-Rt51wPq8Ud"
SLACK_TOKEN = "slack-token-Ke73mHy2Nb"
GMAIL_SECRET = "gmail-client-secret-Zx19aLc6Vo"
NAMECHEAP_KEY = "namecheap-key-Fs64gTj0Wr"
GODADDY_KEY = "godaddy-key-Bq28eNd5Xi"
GODADDY_SECRET = "godaddy-secret-Ya47hMk3Cz"
WORDPRESS_SECRET = "wordpress-secret-Lu95pRv1Ej"
WORDPRESS_TOKEN = "wordpress-access-Oh36tSb7Dn"
CONTACT_EMAIL = "registrant-Gw82@example.com"
ALL_SECRETS = [
    ZOOM_SECRET,
    SLACK_TOKEN,
    GMAIL_SECRET,
    NAMECHEAP_KEY,
    GODADDY_KEY,
    GODADDY_SECRET,
    WORDPRESS_SECRET,
    WORDPRESS_TOKEN,
    CONTACT_EMAIL,
]
CONTACT = {
    "first_name": "Ada",
    "last_name": "Example",
    "address1": "1 Example Way",
    "city": "Springfield",
    "state_province": "IL",
    "postal_code": "00000",
    "country": "US",
    "phone": "+1.5555550100",
    "email": CONTACT_EMAIL,
}

# id -> secret names, for every row that carries an envelope. m365 has none.
ROWS_WITH_SECRETS = {
    "zoom": {"client_secret": ZOOM_SECRET},
    "slack_work": {"token": SLACK_TOKEN},
    "gmail_personal": {"client_secret": GMAIL_SECRET},
    "namecheap_main": {"api_key": NAMECHEAP_KEY, "registrant_contact": json.dumps(CONTACT, sort_keys=True)},
    "godaddy_main": {"api_key": GODADDY_KEY, "api_secret": GODADDY_SECRET},
    "wordpress_blog": {"client_secret": WORDPRESS_SECRET, "access_token": WORDPRESS_TOKEN},
}


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "test.db")
    SQLModel.metadata.create_all(engine)
    yield engine
    connections.set_vault(None)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """No .env file and no inherited key: each test sets exactly the key list it means to use."""
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.delenv("SIGNALSLATE_SECRET_KEY", raising=False)

    def set_keys(*keys: str) -> None:
        monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", ",".join(keys))

    return set_keys


def _populate(vault: Vault) -> None:
    """One connection of every kind that stores a secret, plus m365 which stores none."""
    connections.set_vault(vault)
    connections.create(
        "m365",
        {"alias": "Contoso-1", "tenant_id": "11111111-2222-3333-4444-555555555555", "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
    )
    connections.create("zoom", {"account_id": "acct_Example123", "client_id": "zoomClient_Ex", "client_secret": ZOOM_SECRET})
    connections.create("slack", {"label": "work", "token": SLACK_TOKEN})
    connections.create(
        "gmail", {"label": "personal", "client_id": "1234-example.apps.googleusercontent.com", "client_secret": GMAIL_SECRET}
    )
    connections.create(
        "namecheap",
        {
            "label": "main",
            "api_user": "exampleuser",
            "username": "exampleuser",
            "client_ip": "203.0.113.7",
            "sandbox": "true",
            "api_key": NAMECHEAP_KEY,
            "registrant_contact": CONTACT,
        },
    )
    connections.create(
        "godaddy", {"label": "main", "auth_mode": "classic", "api_key": GODADDY_KEY, "api_secret": GODADDY_SECRET}
    )
    connections.create(
        "wordpress",
        {
            "label": "blog",
            "client_id": "12345",
            "client_secret": WORDPRESS_SECRET,
            "access_token": WORDPRESS_TOKEN,
        },
    )


def _ciphertexts(engine) -> dict[str, str | None]:
    with Session(engine) as session:
        return {row.id: row.secret_ciphertext for row in session.exec(select(db.Connection))}


def _updated_ats(engine) -> dict:
    with Session(engine) as session:
        return {row.id: row.updated_at for row in session.exec(select(db.Connection))}


def _assert_secrets_readable_under(vault: Vault, engine) -> None:
    for connection_id, ciphertext in _ciphertexts(engine).items():
        if connection_id in ROWS_WITH_SECRETS:
            assert ciphertext is not None
            assert vault.decrypt_json(ciphertext) == ROWS_WITH_SECRETS[connection_id]


def _no_leak(text: str, *extra: str) -> None:
    for needle in [*ALL_SECRETS, *extra]:
        assert needle not in text


def test_rotate_all_moves_every_kind_to_the_primary_key(temp_db):
    key_a, key_b = crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    before = _ciphertexts(temp_db)

    result = connections.rotate_all(Vault([key_b, key_a]))

    assert result == RotationResult(rotated=len(ROWS_WITH_SECRETS), without_secrets=1, unreadable=())
    after = _ciphertexts(temp_db)
    for connection_id in ROWS_WITH_SECRETS:
        assert after[connection_id] != before[connection_id]
    # m365 has no envelope and stays exactly as it was.
    assert after["m365_Contoso-1"] is None
    # The whole point: the old key can now be dropped and every secret still opens.
    _assert_secrets_readable_under(Vault([key_b]), temp_db)


def test_rotate_all_is_idempotent_and_leaves_updated_at_alone(temp_db):
    key_a, key_b = crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    stamps = _updated_ats(temp_db)
    keys = Vault([key_b, key_a])

    first = connections.rotate_all(keys)
    second = connections.rotate_all(keys)

    assert first == second == RotationResult(len(ROWS_WITH_SECRETS), 1, ())
    assert _updated_ats(temp_db) == stamps
    _assert_secrets_readable_under(Vault([key_b]), temp_db)


def test_rotate_all_with_an_unreadable_row_writes_nothing_and_reports_its_id(temp_db):
    key_a, key_b, foreign = crypto.generate_key(), crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    with Session(temp_db) as session:
        row = session.get(db.Connection, "slack_work")
        assert row is not None
        row.secret_ciphertext = Vault([foreign]).encrypt_json({"token": SLACK_TOKEN})
        session.add(row)
        session.commit()
    before = _ciphertexts(temp_db)

    result = connections.rotate_all(Vault([key_b, key_a]))

    assert result == RotationResult(rotated=0, without_secrets=1, unreadable=("slack_work",))
    assert _ciphertexts(temp_db) == before


def test_rotate_all_reports_every_unreadable_id_in_creation_order(temp_db):
    key_a, key_b, foreign = crypto.generate_key(), crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    with Session(temp_db) as session:
        for connection_id in ("godaddy_main", "zoom"):
            row = session.get(db.Connection, connection_id)
            assert row is not None
            row.secret_ciphertext = Vault([foreign]).encrypt_json({"x": "y"})
            session.add(row)
        session.commit()

    result = connections.rotate_all(Vault([key_b, key_a]))

    assert result.unreadable == ("zoom", "godaddy_main")
    assert result.rotated == 0


def test_rotate_all_with_no_rows_rotates_nothing(temp_db):
    assert connections.rotate_all(Vault([crypto.generate_key()])) == RotationResult(0, 0, ())


def test_rotate_all_uses_the_installed_vault_by_default_and_needs_one(temp_db):
    with pytest.raises(SecretKeyMissing):
        connections.rotate_all()
    key_a, key_b = crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    connections.set_vault(Vault([key_b, key_a]))

    assert connections.rotate_all().rotated == len(ROWS_WITH_SECRETS)
    _assert_secrets_readable_under(Vault([key_b]), temp_db)


def test_cli_rotates_and_prints_only_counts(temp_db, env, capsys):
    key_a, key_b = crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    before = _ciphertexts(temp_db)
    env(key_b, key_a)

    assert crypto.main(["rotate"]) == 0

    captured = capsys.readouterr()
    assert captured.out == f"rotated {len(ROWS_WITH_SECRETS)} connection(s) to the primary key; 1 had no stored secrets.\n"
    assert captured.err == ""
    _no_leak(captured.out + captured.err, key_a, key_b, *[c for c in before.values() if c])
    _assert_secrets_readable_under(Vault([key_b]), temp_db)


def test_cli_second_run_succeeds(temp_db, env, capsys):
    key_a, key_b = crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    env(key_b, key_a)

    assert crypto.main(["rotate"]) == 0
    capsys.readouterr()
    assert crypto.main(["rotate"]) == 0
    assert "rotated" in capsys.readouterr().out
    _assert_secrets_readable_under(Vault([key_b]), temp_db)


def test_cli_with_no_rows_reports_zero(temp_db, env, capsys):
    env(crypto.generate_key())

    assert crypto.main(["rotate"]) == 0

    assert capsys.readouterr().out == "rotated 0 connection(s) to the primary key; 0 had no stored secrets.\n"


def test_cli_unreadable_row_exits_1_names_only_the_id_and_writes_nothing(temp_db, env, capsys):
    key_a, key_b = crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    before = _ciphertexts(temp_db)
    # The owner dropped key A too early: every row is unreadable under B alone.
    env(key_b)

    assert crypto.main(["rotate"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    for connection_id in ROWS_WITH_SECRETS:
        assert connection_id in captured.err
    assert "nothing was changed" in captured.err
    assert _ciphertexts(temp_db) == before
    _no_leak(captured.out + captured.err, key_a, key_b, *[c for c in before.values() if c])


def test_cli_no_key_exits_2_without_opening_the_database(temp_db, env, monkeypatch, capsys):
    def boom() -> None:
        raise AssertionError("the database must not be opened without a key")

    monkeypatch.setattr(db, "init_db", boom)

    assert crypto.main(["rotate"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SIGNALSLATE_SECRET_KEY" in captured.err


def test_cli_blank_key_exits_2(temp_db, env, monkeypatch, capsys):
    monkeypatch.setenv("SIGNALSLATE_SECRET_KEY", " , ")

    assert crypto.main(["rotate"]) == 2
    assert capsys.readouterr().out == ""


def test_cli_invalid_key_exits_2_naming_position_never_text(temp_db, env, capsys):
    good = crypto.generate_key()
    bad = "not-a-fernet-key-Mv40qXe9"
    env(good, bad)

    assert crypto.main(["rotate"]) == 2

    captured = capsys.readouterr()
    assert "position 2" in captured.err
    _no_leak(captured.out + captured.err, good, bad)


@pytest.mark.parametrize("argv", [[], ["bogus"], ["rotate", "now"], ["genkey", "rotate"]])
def test_cli_usage_mentions_both_commands_and_prints_no_key(argv, capsys):
    assert crypto.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "genkey" in captured.err and "rotate" in captured.err


def test_genkey_is_unchanged(capsys):
    assert crypto.main(["genkey"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    Vault([lines[0]])
    assert "separately from data/" in lines[1]


def test_cli_database_failure_never_prints_the_ciphertext(temp_db, env, monkeypatch, capsys):
    key_a, key_b = crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    ciphertexts = [c for c in _ciphertexts(temp_db).values() if c]
    env(key_b, key_a)

    def locked(self, *args, **kwargs):
        # SQLAlchemy's own message embeds the statement parameters: here, the new ciphertexts.
        raise OperationalError("UPDATE connection SET secret_ciphertext=?", tuple(ciphertexts), Exception("database is locked"))

    monkeypatch.setattr(Session, "commit", locked)

    assert crypto.main(["rotate"]) == 1

    captured = capsys.readouterr()
    assert "OperationalError" in captured.err and "nothing was changed" in captured.err
    _no_leak(captured.out + captured.err, key_a, key_b, *ciphertexts)


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_python_dash_m_entry_point_rotates_through_the_real_module(temp_db, env, monkeypatch, capsys):
    """
    Run as __main__ the file is a second copy of the module, so its exception classes differ from
    the ones connections catches: the entry point must hand off to the imported one.
    """
    key_a, key_b = crypto.generate_key(), crypto.generate_key()
    _populate(Vault([key_a]))
    env(key_b, key_a)
    monkeypatch.setattr(sys, "argv", ["pipeline.crypto", "rotate"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("pipeline.crypto", run_name="__main__")

    assert exit_info.value.code == 0
    assert "rotated" in capsys.readouterr().out
    _assert_secrets_readable_under(Vault([key_b]), temp_db)
