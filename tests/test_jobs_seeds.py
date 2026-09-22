"""
Tests for pipeline.jobs.seeds: watchlist add_company()/import_csv(), and the yc/hn discovery
importers. Real (in-memory) SQLite session per tests/test_job_tables.py's pattern; the two
HTTP-backed importers get a stubbed fetch_json (no real network) per the task's SCOPE_FILES note.
"""
import sys
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import JobCompany  # noqa: E402
from pipeline.jobs import seeds  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# --------------------------------------------------------------------------------------------
# add_company()
# --------------------------------------------------------------------------------------------

def test_add_company_creates_new_active_row(session):
    row = seeds.add_company(session, "Acme Corp", domain="acme.com", source="watchlist")
    session.commit()
    assert row.id is not None
    assert (row.name, row.domain, row.source, row.status) == ("Acme Corp", "acme.com", "watchlist", "active")


def test_add_company_upserts_by_domain_case_insensitive(session):
    first = seeds.add_company(session, "Acme Corp", domain="Acme.com", source="watchlist")
    session.commit()
    first_seen = first.first_seen

    second = seeds.add_company(session, "Acme Corporation", domain="ACME.COM", source="yc")
    session.commit()

    assert second.id == first.id
    assert second.first_seen == first_seen
    rows = session.exec(select(JobCompany)).all()
    assert len(rows) == 1


def test_add_company_upserts_by_name_case_insensitive_when_no_domain_match(session):
    first = seeds.add_company(session, "Acme Corp", source="watchlist")
    session.commit()

    second = seeds.add_company(session, "acme corp", domain="acme.com", source="hn")
    session.commit()

    assert second.id == first.id
    rows = session.exec(select(JobCompany)).all()
    assert len(rows) == 1


def test_add_company_updates_last_seen_on_existing_match(session):
    row = seeds.add_company(session, "Acme Corp", source="manual")
    session.commit()
    row.last_seen = row.first_seen  # force a known baseline
    session.add(row)
    session.commit()

    updated = seeds.add_company(session, "Acme Corp", source="manual")
    session.commit()
    assert updated.last_seen >= row.first_seen


def test_add_company_rejects_empty_name(session):
    with pytest.raises(ValueError):
        seeds.add_company(session, "   ", source="watchlist")


def test_add_company_rejects_bad_source(session):
    with pytest.raises(ValueError):
        seeds.add_company(session, "Acme", source="bogus")


# --------------------------------------------------------------------------------------------
# import_csv()
# --------------------------------------------------------------------------------------------

def test_import_csv_adds_names_and_domains(session):
    text = "\n".join([
        "# a comment",
        "",
        "Acme Corp,acme.com",
        "Beta Inc",
    ])
    result = seeds.import_csv(session, text)
    session.commit()

    assert result.added == ["Acme Corp", "Beta Inc"]
    assert result.rejected == []
    rows = {row.name: row.domain for row in session.exec(select(JobCompany)).all()}
    assert rows == {"Acme Corp": "acme.com", "Beta Inc": None}


def test_import_csv_rejects_bad_row_without_failing_others(session):
    text = "\n".join([
        "Acme Corp",
        ",bad.com",
        "too,many,fields,here",
    ])
    result = seeds.import_csv(session, text)
    session.commit()

    assert result.added == ["Acme Corp"]
    assert [line for line, _reason in result.rejected] == [2, 3]


def test_import_csv_sets_source_watchlist(session):
    result = seeds.import_csv(session, "Acme Corp")
    session.commit()
    row = session.exec(select(JobCompany).where(JobCompany.name == "Acme Corp")).one()
    assert row.source == "watchlist"
    assert result.added == ["Acme Corp"]


# --------------------------------------------------------------------------------------------
# import_yc()
# --------------------------------------------------------------------------------------------

def test_import_yc_adds_companies_with_derived_domain(session):
    def fake_client(url, **kwargs):
        assert url == seeds._YC_HIRING_URL
        return (
            [
                {"name": "Acme Corp", "website": "https://acme.com/careers"},
                {"name": "Beta Inc"},
                {"website": "https://nomissingname.example"},  # skipped: no name
                "not-a-dict",  # skipped: malformed entry
            ],
            "ok",
            None,
        )

    result = seeds.import_yc(session, client=fake_client)
    session.commit()

    assert result.added == ["Acme Corp", "Beta Inc"]
    rows = {row.name: row.domain for row in session.exec(select(JobCompany)).all()}
    assert rows == {"Acme Corp": "acme.com", "Beta Inc": None}


def test_import_yc_caps_at_max_import(session, monkeypatch):
    monkeypatch.setattr(seeds, "_MAX_YC_IMPORT", 2)

    def fake_client(url, **kwargs):
        return ([{"name": f"Company {i}"} for i in range(5)], "ok", None)

    result = seeds.import_yc(session, client=fake_client)
    session.commit()
    assert len(result.added) == 2


def test_import_yc_degrades_on_fetch_failure(session):
    def fake_client(url, **kwargs):
        return None, "unavailable", "ConnectionError"

    result = seeds.import_yc(session, client=fake_client)
    assert result.added == []
    assert result.rejected == []


def test_import_yc_degrades_on_unexpected_shape(session):
    def fake_client(url, **kwargs):
        return {"not": "a list"}, "ok", None

    result = seeds.import_yc(session, client=fake_client)
    assert result.added == []


# --------------------------------------------------------------------------------------------
# import_hn()
# --------------------------------------------------------------------------------------------

def test_import_hn_adds_companies_and_hints(session, monkeypatch):
    def fake_fetch_json(url, **kwargs):
        if url == seeds._HN_LATEST_THREAD_URL:
            assert kwargs.get("params") == {"tags": "story,author_whoishiring", "hitsPerPage": 1}
            return {"hits": [{"objectID": "123"}]}, "ok", None
        if url == seeds._HN_ITEM_URL.format(object_id="123"):
            return (
                {
                    "children": [
                        {
                            "text": (
                                'Acme Corp | Remote | We are hiring. '
                                '<a href="https://boards.greenhouse.io/acme">apply here</a>'
                            )
                        },
                        {"text": None},  # skipped: no text (e.g. deleted comment)
                        "not-a-dict",  # skipped: malformed comment
                    ]
                },
                "ok",
                None,
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(seeds, "fetch_json", fake_fetch_json)

    result = seeds.import_hn(session)
    session.commit()

    assert result.added == ["Acme Corp"]
    assert result.hints == [("Acme Corp", "greenhouse", "acme")]


def test_import_hn_degrades_when_search_fails(session, monkeypatch):
    def fake_fetch_json(url, **kwargs):
        return None, "unavailable", "Timeout"

    monkeypatch.setattr(seeds, "fetch_json", fake_fetch_json)
    result = seeds.import_hn(session)
    assert result.added == []
    assert result.hints == []


def test_import_hn_degrades_when_no_hits(session, monkeypatch):
    def fake_fetch_json(url, **kwargs):
        return {"hits": []}, "ok", None

    monkeypatch.setattr(seeds, "fetch_json", fake_fetch_json)
    result = seeds.import_hn(session)
    assert result.added == []


def test_import_hn_degrades_when_item_fetch_fails(session, monkeypatch):
    def fake_fetch_json(url, **kwargs):
        if url == seeds._HN_LATEST_THREAD_URL:
            return {"hits": [{"objectID": "123"}]}, "ok", None
        return None, "error", "HTTP500"

    monkeypatch.setattr(seeds, "fetch_json", fake_fetch_json)
    result = seeds.import_hn(session)
    assert result.added == []


def test_import_hn_caps_at_max_comments(session, monkeypatch):
    monkeypatch.setattr(seeds, "_MAX_HN_COMMENTS", 2)

    def fake_fetch_json(url, **kwargs):
        if url == seeds._HN_LATEST_THREAD_URL:
            return {"hits": [{"objectID": "123"}]}, "ok", None
        return (
            {"children": [{"text": f"Company{i} | Remote"} for i in range(5)]},
            "ok",
            None,
        )

    monkeypatch.setattr(seeds, "fetch_json", fake_fetch_json)
    result = seeds.import_hn(session)
    session.commit()
    assert len(result.added) == 2
