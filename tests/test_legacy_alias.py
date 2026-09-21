"""
Installs that predate the management UI keep working with no key configured.

origin/main built the MSAL cache path from any alias, so M365_ORG1_ALIAS=acme_corp or acme.com with
tokens/<alias>_cache.bin worked. Narrowing tokencache to [A-Za-z0-9-]+ made tokencache.cache_path raise
inside check_m365, which aborted check_all_configured, so the whole run failed for such an install even
though nothing about it was wrong. The alias rule is now a safe filename class; the stricter rule for
NEW UI-created connections lives in pipeline/connections.py and is not touched.

Real files, real msal caches, the real runner and a real (temp) database. Only the MSAL app (a real
one would call login.microsoftonline.com) and the Graph resource collectors (they would call
graph.microsoft.com) are stood in for; token cache reads and writes, health and dispatch are real.
"""
import os
import stat
import sys
from pathlib import Path

import msal
import pytest
from sqlmodel import SQLModel, create_engine, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import config_store, db, health, runner, tokencache  # noqa: E402
from pipeline.clock import utcnow  # noqa: E402
from pipeline.collectors import Item, graph  # noqa: E402

ACCESS = "eyJ-not-a-real-access-token"
REFRESH = "0.not-a-real-refresh-token"
LEGACY_ALIASES = ["acme_corp", "acme.com", "Acme-Corp_2.eu"]
REFUSED_ALIASES = ["../evil", "a/b", "a\\b", ".hidden", "..", "", "a\n", "a b", "é", "x" * 65]
# The subset a single .env line can carry: an empty value is "not declared" and a newline splits the line.
DECLARABLE_REFUSED = [a for a in REFUSED_ALIASES if a and "\n" not in a]


def _env(*aliases: str) -> str:
    lines = ["M365_CLIENT_ID=cid"]
    for n, alias in enumerate(aliases, start=1):
        lines += [f"M365_ORG{n}_ALIAS={alias}", f"M365_ORG{n}_TENANT_ID=tid{n}"]
    return "\n".join(lines) + "\n"


def _cache_with_token() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    cache.add({
        "client_id": "00000000-0000-0000-0000-000000000000",
        "scope": ["Mail.Read"],
        "token_endpoint": "https://login.example.com/tid/oauth2/v2.0/token",
        "response": {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600},
        "data": {},
    })
    return cache


def _seed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_cache_with_token().serialize())
    os.chmod(path, 0o600)


class OkApp:
    """Stands in for msal.PublicClientApplication: one account, a good silent refresh."""

    def __init__(self, cache):
        self.cache = cache

    def get_accounts(self):
        return [{"home_account_id": "acct"}]

    def acquire_token_silent_with_error(self, scopes, account=None):
        return {"access_token": ACCESS, "expires_in": 3600}


@pytest.fixture
def install(monkeypatch, tmp_path):
    """
    No SIGNALSLATE_SECRET_KEY (conftest strips it) and no vault overlay: the .env alone declares the
    aliases. health.TOKEN_DIR points at tmp_path/tokens; tokencache's own default is left alone on
    purpose, so a call that forgot to pass the directory would fail these tests.
    """
    token_dir = tmp_path / "tokens"
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(health, "TOKEN_DIR", token_dir)
    monkeypatch.setattr(health, "m365_app", lambda cfg, cache: OkApp(cache))
    monkeypatch.setattr(graph, "m365_app", lambda cfg, cache: OkApp(cache))

    def declare(*aliases: str) -> Path:
        (tmp_path / ".env").write_text(_env(*aliases))
        return token_dir

    return declare


# ---- the bug: no key, legacy aliases ------------------------------------------------------------


def test_check_all_configured_does_not_raise_for_legacy_aliases_without_a_cache(install):
    install("acme_corp", "acme.com")

    results = health.check_all_configured({"m365_acme_corp": True, "m365_acme.com": True})

    assert [r.source for r in results] == ["m365_acme_corp", "m365_acme.com"]
    assert all(r.status == "error" and "No token cache" in r.detail for r in results)


@pytest.mark.parametrize("alias", LEGACY_ALIASES)
def test_check_m365_finds_a_preexisting_cache_file_for_a_legacy_alias(install, alias):
    token_dir = install(alias)
    _seed(token_dir / f"{alias}_cache.bin")

    result = health.check_m365(alias)

    assert result.status == "ok", result.detail
    assert result.source == f"m365_{alias}"


def test_check_all_configured_reports_two_legacy_aliases_ok_from_their_cache_files(install):
    token_dir = install("acme_corp", "acme.com")
    _seed(token_dir / "acme_corp_cache.bin")
    _seed(token_dir / "acme.com_cache.bin")

    results = health.check_all_configured({"m365_acme_corp": True, "m365_acme.com": True})

    assert [(r.source, r.status) for r in results] == [("m365_acme_corp", "ok"), ("m365_acme.com", "ok")]


@pytest.mark.parametrize("alias", LEGACY_ALIASES)
def test_graph_access_token_reads_a_legacy_alias_cache(install, alias):
    token_dir = install(alias)
    _seed(token_dir / f"{alias}_cache.bin")

    assert graph.access_token(alias) == ACCESS


# ---- traversal and malformed aliases are still refused -------------------------------------------


@pytest.mark.parametrize("alias", REFUSED_ALIASES)
def test_tokencache_still_refuses_traversal_and_malformed_aliases(tmp_path, alias):
    cache = msal.SerializableTokenCache()
    cache.has_state_changed = True
    calls = [
        lambda: tokencache.cache_path(alias, token_dir=tmp_path),
        lambda: tokencache.alias_lock(alias),
        lambda: tokencache.locked(alias, token_dir=tmp_path).__enter__(),
        lambda: tokencache.load(alias, token_dir=tmp_path),
        lambda: tokencache.save(alias, cache, token_dir=tmp_path),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call()
    assert list(tmp_path.iterdir()) == []


def test_a_64_character_alias_is_the_longest_accepted(tmp_path):
    assert tokencache.cache_path("a" * 64, token_dir=tmp_path).name == f"{'a' * 64}_cache.bin"
    with pytest.raises(ValueError):
        tokencache.cache_path("a" * 65, token_dir=tmp_path)


@pytest.mark.parametrize("alias", DECLARABLE_REFUSED)
def test_check_m365_turns_a_refused_alias_into_an_error_result_and_writes_nothing(install, tmp_path, alias):
    token_dir = install(alias)

    result = health.check_m365(alias)

    assert result.status == "error"
    assert "Alias cannot name a token cache file" in result.detail
    assert not token_dir.exists()
    assert not (tmp_path.parent / "evil_cache.bin").exists()


def test_a_refused_alias_does_not_stop_the_other_sources_health_checks(install):
    token_dir = install("../evil", "acme_corp")
    _seed(token_dir / "acme_corp_cache.bin")

    results = health.check_all_configured({"m365_../evil": True, "m365_acme_corp": True})

    assert [(r.source, r.status) for r in results] == [("m365_../evil", "error"), ("m365_acme_corp", "ok")]


# ---- atomic 0600 write and corrupt-file tolerance hold for the widened aliases --------------------


@pytest.mark.parametrize("alias", LEGACY_ALIASES)
def test_save_is_atomic_and_0600_for_a_legacy_alias(tmp_path, alias):
    old_umask = os.umask(0o000)
    try:
        tokencache.save(alias, _cache_with_token(), token_dir=tmp_path)
    finally:
        os.umask(old_umask)

    path = tmp_path / f"{alias}_cache.bin"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # the temp file was replaced away
    assert tokencache.load(alias, token_dir=tmp_path) is not None


@pytest.mark.parametrize("content", ["", "{", "[]", "not json"])
def test_a_corrupt_legacy_cache_is_an_error_result_not_an_exception(install, content):
    token_dir = install("acme_corp")
    token_dir.mkdir()
    (token_dir / "acme_corp_cache.bin").write_text(content)

    results = health.check_all_configured({"m365_acme_corp": True})

    assert [(r.source, r.status) for r in results] == [("m365_acme_corp", "error")]


def test_the_alias_lock_is_one_object_per_legacy_alias():
    assert tokencache.alias_lock("acme_corp") is tokencache.alias_lock("acme_corp")
    assert tokencache.alias_lock("acme_corp") is not tokencache.alias_lock("acme.com")


# ---- a run with one bad alias and one good source ------------------------------------------------


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


def test_a_run_with_one_bad_alias_and_one_good_source_collects_the_good_one(install, temp_db, monkeypatch, tmp_path):
    token_dir = install("../evil", "acme_corp")
    _seed(token_dir / "acme_corp_cache.bin")
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "data" / "config.json")
    # zoom is always declared; keep its network health check out of this run.
    config_store.set_source_active("zoom", False)
    seen = utcnow()
    monkeypatch.setattr(
        graph, "COLLECTORS",
        {"mail": lambda token, since, until: [Item("message", "legacy-1", seen, {"text": "hi"})]},
    )

    run = runner.execute_run()

    assert run.id is not None and run.status != "running"
    with db.get_session() as session:
        stored = list(session.exec(select(db.CollectedItem)).all())
    assert [(i.source, i.external_id) for i in stored] == [("m365_acme_corp", "legacy-1")]
    by_source = {h.source: h.status for h in db.source_health_for_run(run.id)}
    assert by_source == {"m365_../evil": "error", "m365_acme_corp": "ok"}
    assert db.get_cursor("m365_acme_corp") is not None
