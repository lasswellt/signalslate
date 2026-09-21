"""
check_m365 and the Graph collector's access_token both go through pipeline.tokencache.

Real files in tmp_path, real msal.SerializableTokenCache, real threads. The only stand-in is the MSAL
app (m365_app), because a real one would call login.microsoftonline.com; the stub mutates the real
cache the way a refresh does, so persistence is exercised for real.

Two traps pinned here:
  - tokencache has its own TOKEN_DIR while tests and callers repoint health.TOKEN_DIR, so an
    unwired call would silently read and write the real repo tokens/ directory.
  - a corrupt cache must become a per-source error, not an exception out of check_all_configured.
"""
import os
import stat
import sys
import threading
from datetime import datetime
from pathlib import Path

import msal
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import health, tokencache  # noqa: E402
from pipeline.collectors import graph  # noqa: E402

ACCESS = "eyJ-not-a-real-access-token"
REFRESH = "0.not-a-real-refresh-token"
ENV = "M365_CLIENT_ID=cid\nM365_ORG1_ALIAS=work\nM365_ORG1_TENANT_ID=tid\n"
REAL_TOKENS = ROOT / "tokens"


def _snapshot(path: Path):
    """Existence plus every entry's mtime: enough to notice any write into the real directory."""
    if not path.exists():
        return None
    return sorted((p.name, p.stat().st_mtime_ns) for p in path.iterdir())


def _seed(path: Path, n: int = 0, mode: int = 0o600) -> None:
    """Write a valid cache file holding one access token, through MSAL's own serializer."""
    cache = msal.SerializableTokenCache()
    _add_token(cache, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cache.serialize())
    os.chmod(path, mode)


def _add_token(cache: msal.SerializableTokenCache, n: int) -> None:
    cache.add({
        "client_id": "00000000-0000-0000-0000-000000000000",
        "scope": [f"Mail.Read{n}"],
        "token_endpoint": "https://login.example.com/tid/oauth2/v2.0/token",
        "response": {"access_token": f"{ACCESS}-{n}", "refresh_token": REFRESH, "expires_in": 3600},
        "data": {},
    })


def _token_count(cache: msal.SerializableTokenCache) -> int:
    return len(list(cache.search(msal.TokenCache.CredentialType.ACCESS_TOKEN)))


def _saved_token_count(token_dir: Path) -> int:
    cache = tokencache.load("work", token_dir=token_dir)
    assert cache is not None
    return _token_count(cache)


class ProbeApp:
    """
    Stands in for msal.PublicClientApplication. Records whether the alias lock was held when it was
    asked to refresh, and adds one token to the shared cache the way a real refresh would.
    """

    def __init__(self, cache, alias, events, gate=None):
        self.cache = cache
        self.alias = alias
        self.events = events
        self.gate = gate

    def get_accounts(self):
        return [{"home_account_id": "acct"}]

    def acquire_token_silent_with_error(self, scopes, account=None):
        lock_held = tokencache.alias_lock(self.alias).locked()
        self.events.append(("refresh", threading.current_thread().name, lock_held))
        if self.gate is not None:
            self.gate()
        _add_token(self.cache, _token_count(self.cache))
        return {"access_token": ACCESS, "expires_in": 3600}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """
    .env in tmp_path, health.TOKEN_DIR at tmp_path/tokens, tokencache.TOKEN_DIR left at the real
    default on purpose. Teardown proves the real repo tokens/ directory was never touched.
    """
    (tmp_path / ".env").write_text(ENV)
    token_dir = tmp_path / "tokens"
    monkeypatch.setattr(health, "ROOT", tmp_path)
    monkeypatch.setattr(health, "TOKEN_DIR", token_dir)
    assert tokencache.TOKEN_DIR == REAL_TOKENS
    before = _snapshot(REAL_TOKENS)

    events: list = []

    def install(gate=None):
        def factory(cfg, cache):
            events.append(("app", threading.current_thread().name, _token_count(cache)))
            return ProbeApp(cache, cfg["alias"], events, gate)

        monkeypatch.setattr(health, "m365_app", factory)
        monkeypatch.setattr(graph, "m365_app", factory)

    install()
    yield token_dir / "work_cache.bin", events, install
    assert _snapshot(REAL_TOKENS) == before, "the real repo tokens/ directory was touched"


# ---- the patched health.TOKEN_DIR is honored ---------------------------------------------------


def test_m365_cache_path_follows_a_patched_health_token_dir(wired):
    path, _, _ = wired
    assert health.m365_cache_path("work") == path


def test_check_and_collect_use_health_token_dir_and_never_the_real_directory(wired):
    path, events, _ = wired
    _seed(path)

    assert health.check_m365("work").status == "ok"
    assert graph.access_token("work") == ACCESS

    # both refreshes saw and extended the file in the patched directory ...
    assert [e[2] for e in events if e[0] == "app"] == [1, 2]
    assert _saved_token_count(path.parent) == 3
    # ... and the wired fixture's teardown asserts the real tokens/ directory is unchanged.


def test_missing_cache_keeps_the_existing_message_in_both_callers(wired):
    expected = "No token cache — run auth/m365_bootstrap.py once on a machine with a browser"

    result = health.check_m365("work")
    assert (result.status, result.detail) == ("error", expected)
    with pytest.raises(graph.GraphError) as exc:
        graph.access_token("work")
    assert str(exc.value) == expected


# ---- a corrupt or unreadable cache degrades to an error, never an exception ---------------------


@pytest.mark.parametrize("content", [
    '{"AccessToken": {"k": {"secret": "' + ACCESS,  # truncated mid-write
    "",
    "not json at all",
    "[]",
])
def test_corrupt_cache_yields_error_results_and_leaks_nothing(wired, content):
    path, events, _ = wired
    path.parent.mkdir(parents=True)
    path.write_text(content)

    result = health.check_m365("work")
    assert (result.status, result.detail) == ("error", "Token cache unreadable: re-run sign-in")

    collected = graph.collect_m365("work", datetime(2026, 9, 12, 6), datetime(2026, 9, 13, 6))
    assert collected.status == "error"
    assert "Token cache unreadable: re-run sign-in" in collected.detail

    assert events == []  # no MSAL app was ever built on a damaged cache
    for text in (result.detail, collected.detail):
        assert ACCESS not in text and REFRESH not in text


def test_corrupt_cache_does_not_break_check_all_configured(wired):
    path, _, _ = wired
    path.parent.mkdir(parents=True)
    path.write_text("{truncated")

    results = health.check_all_configured({"m365_work": True})
    assert [(r.source, r.status) for r in results] == [("m365_work", "error")]


def test_corrupt_cache_file_is_left_for_the_signin_flow_to_replace(wired):
    path, _, _ = wired
    path.parent.mkdir(parents=True)
    path.write_text("{truncated")

    health.check_m365("work")
    assert path.read_text() == "{truncated"


# ---- refreshed state is persisted atomically, mode 0600 ------------------------------------------


@pytest.mark.parametrize("caller", [
    lambda: health.check_m365("work"),
    lambda: graph.access_token("work"),
])
def test_refreshed_state_is_saved_0600_with_no_temp_files(wired, caller):
    path, _, _ = wired
    _seed(path, mode=0o644)  # what the old write_text path left behind

    caller()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == [path.name]
    assert _saved_token_count(path.parent) == 2


def test_unchanged_cache_is_not_rewritten(wired, monkeypatch):
    path, _, _ = wired
    _seed(path)

    class QuietApp(ProbeApp):
        def acquire_token_silent_with_error(self, scopes, account=None):
            return {"access_token": ACCESS, "expires_in": 3600}

    def factory(cfg, cache):
        return QuietApp(cache, cfg["alias"], [])

    monkeypatch.setattr(health, "m365_app", factory)
    before = path.stat().st_mtime_ns
    assert health.check_m365("work").status == "ok"
    assert path.stat().st_mtime_ns == before


# ---- a concurrent check and collect on the same alias serialize -----------------------------------


def test_refresh_runs_under_the_alias_lock_in_both_callers(wired):
    path, events, _ = wired
    _seed(path)

    health.check_m365("work")
    graph.access_token("work")

    assert [e for e in events if e[0] == "refresh"] == [
        ("refresh", threading.current_thread().name, True),
        ("refresh", threading.current_thread().name, True),
    ]


@pytest.mark.parametrize("first,second", [
    (lambda: health.check_m365("work"), lambda: graph.access_token("work")),
    (lambda: graph.access_token("work"), lambda: health.check_m365("work")),
])
def test_concurrent_check_and_collect_serialize_and_lose_no_update(wired, first, second):
    path, events, install = wired
    _seed(path)

    holding = threading.Event()
    release = threading.Event()

    def gate():
        if threading.current_thread().name == "first":
            holding.set()
            assert release.wait(timeout=10)

    install(gate)

    t1 = threading.Thread(target=first, name="first")
    t2 = threading.Thread(target=second, name="second")
    t1.start()
    assert holding.wait(timeout=5)
    t2.start()

    # the second caller must not even load the cache while the first holds the lock
    t2.join(timeout=0.3)
    assert t2.is_alive()
    assert not [e for e in events if e[:2] == ("app", "second")]

    release.set()
    for t in (t1, t2):
        t.join(timeout=10)
        assert not t.is_alive()

    # the second caller loaded after the first saved: it started from 2 tokens, not the seed's 1
    assert [e for e in events if e[:2] == ("app", "second")] == [("app", "second", 2)]
    assert _saved_token_count(path.parent) == 3
