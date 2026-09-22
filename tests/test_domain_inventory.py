"""
Tests for pipeline.domains.inventory: registrar_for(), sync_all(), add_manual/remove/import_csv
and refresh_snapshots().

registrar_for() is exercised against real connections (temp DB + vault, mirroring
tests/test_connections.py) because building a registrar adapter is pure config/secret loading —
no network call happens until list_domains()/check() run. Every other function takes a fake
Registrar or a fake inspect_fn injected through the module's own keyword parameters (registrar_
factory / inspect_fn), never a mock of pipeline.domains.inventory or any sibling module under test.
"""
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, crypto, db  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.db import Domain, DomainSnapshot  # noqa: E402
from pipeline.domains import inventory  # noqa: E402
from pipeline.domains.registrars import RegistrarDomain, RegistrarError  # noqa: E402
from pipeline.domains.registrars.godaddy import GoDaddyClient  # noqa: E402
from pipeline.domains.registrars.namecheap import NamecheapClient  # noqa: E402
from pipeline.domains.registrars.wordpress import WordPressClient  # noqa: E402


# --- fixtures ----------------------------------------------------------------------


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


def _rdomain(name, expires_at=None, auto_renew=True, locked=True, privacy=False, nameservers=()):
    return RegistrarDomain(
        name=name, expires_at=expires_at, auto_renew=auto_renew, locked=locked, privacy=privacy, nameservers=nameservers
    )


class FakeRegistrar:
    """In-process fake Registrar: no network, no pipeline module mocked."""

    kind = "namecheap"

    def __init__(self, domains=(), fail=False):
        self._domains = list(domains)
        self._fail = fail

    def list_domains(self):
        if self._fail:
            raise RegistrarError("boom")
        return list(self._domains)

    def check(self, names):
        raise NotImplementedError

    def purchase(self, quote, years, contact):
        raise NotImplementedError

    def check_connection(self):
        return "ok"


def _factory(mapping):
    """A registrar_factory: connection_id -> the FakeRegistrar (or exception) mapping supplies."""

    def _build(connection_id):
        value = mapping[connection_id]
        if isinstance(value, Exception):
            raise value
        return value

    return _build


# --- registrar_for -------------------------------------------------------------------


def test_registrar_for_builds_namecheap(vault):
    connections.create(
        "namecheap",
        {"label": "main", "api_user": "u", "username": "u", "client_ip": "1.2.3.4", "api_key": "nc-key-Ab12"},
    )
    registrar = inventory.registrar_for("namecheap_main")
    assert isinstance(registrar, NamecheapClient)
    assert registrar.kind == "namecheap"


def test_registrar_for_builds_godaddy(vault):
    connections.create("godaddy", {"label": "main", "api_key": "gd-key", "api_secret": "gd-secret-Zz99"})
    registrar = inventory.registrar_for("godaddy_main")
    assert isinstance(registrar, GoDaddyClient)


def test_registrar_for_builds_wordpress(vault):
    connections.create("wordpress", {"label": "main", "client_id": "12345", "client_secret": "wp-secret-Qw88"})
    connections.set_secret("wordpress_main", "access_token", "wp-token-Xy77")
    registrar = inventory.registrar_for("wordpress_main")
    assert isinstance(registrar, WordPressClient)


def test_registrar_for_unknown_connection_raises(temp_db):
    with pytest.raises(inventory.UnknownRegistrar):
        inventory.registrar_for("no-such-connection")


def test_registrar_for_non_registrar_kind_raises(vault):
    connections.create("slack", {"label": "work", "token": "slack-token-Hd91"})
    with pytest.raises(inventory.UnknownRegistrar):
        inventory.registrar_for("slack_work")


# --- sync_all ------------------------------------------------------------------------


def _connection(session, connection_id, kind="namecheap"):
    conn = db.Connection(id=connection_id, kind=kind, label="main", origin="ui", config="{}")
    session.add(conn)
    session.commit()
    return conn


def test_sync_all_upserts_new_domains(temp_db):
    with Session(temp_db) as session:
        _connection(session, "namecheap_main", "namecheap")

    registrar = FakeRegistrar([_rdomain("example.com"), _rdomain("other.com")])
    statuses = inventory.sync_all(registrar_factory=_factory({"namecheap_main": registrar}))

    assert len(statuses) == 1
    assert statuses[0] == inventory.SyncStatus(
        connection_id="namecheap_main", kind="namecheap", status="ok", detail="synced 2 domain(s)", domain_count=2
    )
    with Session(temp_db) as session:
        rows = {row.name: row for row in session.exec(select(Domain)).all()}
        assert set(rows) == {"example.com", "other.com"}
        assert rows["example.com"].ownership == "owned"
        assert rows["example.com"].source == "namecheap"
        assert rows["example.com"].connection_id == "namecheap_main"
        assert rows["example.com"].missing_since is None


def test_sync_all_sets_missing_since_for_vanished_domain_and_never_deletes(temp_db):
    with Session(temp_db) as session:
        _connection(session, "namecheap_main", "namecheap")

    inventory.sync_all(registrar_factory=_factory({"namecheap_main": FakeRegistrar([_rdomain("example.com"), _rdomain("gone.com")])}))
    inventory.sync_all(registrar_factory=_factory({"namecheap_main": FakeRegistrar([_rdomain("example.com")])}))

    with Session(temp_db) as session:
        rows = {row.name: row for row in session.exec(select(Domain)).all()}
        assert set(rows) == {"example.com", "gone.com"}  # still present, never deleted
        assert rows["gone.com"].missing_since is not None
        assert rows["example.com"].missing_since is None


def test_sync_all_clears_missing_since_when_domain_reappears(temp_db):
    with Session(temp_db) as session:
        _connection(session, "namecheap_main", "namecheap")

    inventory.sync_all(registrar_factory=_factory({"namecheap_main": FakeRegistrar([])}))
    with Session(temp_db) as session:
        # Simulate a domain that vanished on a prior pass.
        session.add(Domain(name="back.com", ownership="owned", source="namecheap", connection_id="namecheap_main", missing_since=utcnow()))
        session.commit()

    inventory.sync_all(registrar_factory=_factory({"namecheap_main": FakeRegistrar([_rdomain("back.com")])}))

    with Session(temp_db) as session:
        row = session.exec(select(Domain).where(Domain.name == "back.com")).one()
        assert row.missing_since is None


def test_sync_all_one_connection_failing_does_not_stop_others(temp_db):
    with Session(temp_db) as session:
        _connection(session, "namecheap_main", "namecheap")
        _connection(session, "godaddy_main", "godaddy")

    statuses = inventory.sync_all(
        registrar_factory=_factory(
            {"namecheap_main": FakeRegistrar([], fail=True), "godaddy_main": FakeRegistrar([_rdomain("ok.com")])}
        )
    )
    by_id = {s.connection_id: s for s in statuses}
    assert by_id["namecheap_main"].status == "error"
    assert by_id["namecheap_main"].detail == "RegistrarError"
    assert by_id["godaddy_main"].status == "ok"
    with Session(temp_db) as session:
        assert session.exec(select(Domain).where(Domain.name == "ok.com")).one() is not None


def test_sync_all_skips_non_registrar_connections(temp_db):
    with Session(temp_db) as session:
        _connection(session, "slack_work", "slack")

    statuses = inventory.sync_all(registrar_factory=_factory({}))
    assert statuses == []


# --- add_manual / remove / import_csv -------------------------------------------------


def test_add_manual_creates_row(temp_db):
    row = inventory.add_manual("Example.COM.", "watched")
    assert row.name == "example.com"
    assert row.ownership == "watched"
    assert row.source == "manual"


def test_add_manual_updates_ownership_without_touching_registrar_fields(temp_db):
    with Session(temp_db) as session:
        _connection(session, "namecheap_main", "namecheap")
    inventory.sync_all(registrar_factory=_factory({"namecheap_main": FakeRegistrar([_rdomain("example.com")])}))

    row = inventory.add_manual("example.com", "watched")
    assert row.ownership == "watched"
    assert row.source == "namecheap"
    assert row.connection_id == "namecheap_main"


def test_add_manual_rejects_invalid_ownership(temp_db):
    with pytest.raises(ValueError):
        inventory.add_manual("example.com", "bogus")


def test_add_manual_rejects_invalid_domain(temp_db):
    with pytest.raises(ValueError):
        inventory.add_manual("not a domain")


def test_remove_marks_missing_since_never_deletes(temp_db):
    inventory.add_manual("example.com")
    assert inventory.remove("example.com") is True
    with Session(temp_db) as session:
        row = session.exec(select(Domain).where(Domain.name == "example.com")).one()
        assert row.missing_since is not None


def test_remove_returns_false_for_unknown_or_already_removed(temp_db):
    assert inventory.remove("nope.com") is False
    inventory.add_manual("example.com")
    inventory.remove("example.com")
    assert inventory.remove("example.com") is False


def test_import_csv_adds_rows_and_reports_rejects(temp_db):
    text = "\n".join(
        [
            "# a comment",
            "",
            "example.com",
            "watched.com,watched",
            "bad domain",
            "toomany.com,owned,extra",
            "example.com,bogus-ownership",
        ]
    )
    result = inventory.import_csv(text)
    assert result.added == ["example.com", "watched.com"]
    assert [line for line, _reason in result.rejected] == [5, 6, 7]

    with Session(temp_db) as session:
        rows = {row.name: row for row in session.exec(select(Domain)).all()}
        assert rows["watched.com"].ownership == "watched"
        assert rows["example.com"].ownership == "owned"  # the later bad-ownership row never applied


# --- refresh_snapshots -----------------------------------------------------------------


def _domain_row(session, name="example.com", missing_since=None):
    row = Domain(name=name, ownership="owned", source="manual", missing_since=missing_since)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _fake_inspect(data_hash, payload=None):
    def _inspect(name):
        return {"dns": payload or {}, "data_hash": data_hash}

    return _inspect


def test_refresh_snapshots_stores_first_snapshot(temp_db):
    with Session(temp_db) as session:
        _domain_row(session)

    statuses = inventory.refresh_snapshots(inspect_fn=_fake_inspect("hash-1"))
    assert statuses == [inventory.SnapshotStatus(name="example.com", status="stored", detail="snapshot stored")]
    with Session(temp_db) as session:
        snaps = session.exec(select(DomainSnapshot)).all()
        assert len(snaps) == 1
        assert snaps[0].data_hash == "hash-1"


def test_refresh_snapshots_skips_unchanged_hash_within_a_day(temp_db):
    with Session(temp_db) as session:
        _domain_row(session)

    inventory.refresh_snapshots(inspect_fn=_fake_inspect("hash-1"))
    statuses = inventory.refresh_snapshots(inspect_fn=_fake_inspect("hash-1"))
    assert statuses == [inventory.SnapshotStatus(name="example.com", status="unchanged", detail="no change since last snapshot")]
    with Session(temp_db) as session:
        assert len(session.exec(select(DomainSnapshot)).all()) == 1


def test_refresh_snapshots_stores_on_hash_change(temp_db):
    with Session(temp_db) as session:
        _domain_row(session)

    inventory.refresh_snapshots(inspect_fn=_fake_inspect("hash-1"))
    statuses = inventory.refresh_snapshots(inspect_fn=_fake_inspect("hash-2"))
    assert statuses[0].status == "stored"
    with Session(temp_db) as session:
        assert len(session.exec(select(DomainSnapshot)).all()) == 2


def test_refresh_snapshots_stores_one_heartbeat_per_day_even_when_unchanged(temp_db):
    with Session(temp_db) as session:
        _domain_row(session)

    now = utcnow()
    inventory.refresh_snapshots(inspect_fn=_fake_inspect("hash-1"), now=now)
    later_same_day = inventory.refresh_snapshots(inspect_fn=_fake_inspect("hash-1"), now=now + timedelta(hours=2))
    assert later_same_day[0].status == "unchanged"

    next_day = inventory.refresh_snapshots(inspect_fn=_fake_inspect("hash-1"), now=now + timedelta(days=1, minutes=1))
    assert next_day[0].status == "stored"
    with Session(temp_db) as session:
        assert len(session.exec(select(DomainSnapshot)).all()) == 2


def test_refresh_snapshots_skips_missing_domains(temp_db):
    with Session(temp_db) as session:
        _domain_row(session, missing_since=utcnow())

    statuses = inventory.refresh_snapshots(inspect_fn=_fake_inspect("hash-1"))
    assert statuses == []
    with Session(temp_db) as session:
        assert session.exec(select(DomainSnapshot)).all() == []


def test_refresh_snapshots_records_error_and_does_not_store(temp_db):
    with Session(temp_db) as session:
        _domain_row(session)

    def _raising(name):
        raise ValueError("dns exploded")

    statuses = inventory.refresh_snapshots(inspect_fn=_raising)
    assert statuses == [inventory.SnapshotStatus(name="example.com", status="error", detail="ValueError")]
    with Session(temp_db) as session:
        assert session.exec(select(DomainSnapshot)).all() == []
