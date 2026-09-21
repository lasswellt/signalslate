"""
Unit tests for pipeline.tokencache. Real files in tmp_path, real msal.SerializableTokenCache, real
threads; the only patched thing is os.replace, to inject a failure between the write and the rename.
Secret discipline: token strings must never surface in a log record or an exception message.
"""
import json
import logging
import os
import stat
import sys
import threading
from pathlib import Path

import msal
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import tokencache  # noqa: E402

SECRET = "eyJ-not-a-real-access-token"
REFRESH = "0.not-a-real-refresh-token"


@pytest.fixture
def token_dir(tmp_path, monkeypatch):
    d = tmp_path / "tokens"
    monkeypatch.setattr(tokencache, "TOKEN_DIR", d)
    return d


def _add_token(cache: msal.SerializableTokenCache, n: int = 0, secret: str = SECRET) -> None:
    """Put one access token into the cache through MSAL's own add(), which flips has_state_changed."""
    cache.add({
        "client_id": "00000000-0000-0000-0000-000000000000",
        "scope": [f"Mail.Read{n}"],
        "token_endpoint": "https://login.example.com/tenant-a/oauth2/v2.0/token",
        "response": {"access_token": f"{secret}-{n}", "refresh_token": REFRESH, "expires_in": 3600},
        "data": {},
    })


def _changed_cache(n: int = 0) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    _add_token(cache, n)
    assert cache.has_state_changed
    return cache


def _access_token_count(cache: msal.SerializableTokenCache) -> int:
    return len(list(cache.search(msal.TokenCache.CredentialType.ACCESS_TOKEN)))


# ---- cache_path -----------------------------------------------------------------------------

def test_cache_path_is_inside_token_dir_and_keeps_existing_filename(token_dir):
    assert tokencache.cache_path("tenant-a") == token_dir / "tenant-a_cache.bin"


def test_default_token_dir_is_repo_tokens_directory():
    assert tokencache.TOKEN_DIR == ROOT / "tokens"


@pytest.mark.parametrize("alias", [
    "", "..", "../x", "a/b", "a\\b", "/etc/passwd", "a b", "a\n", "a\0b", "é",
])
def test_cache_path_rejects_invalid_alias(token_dir, alias):
    with pytest.raises(ValueError) as exc:
        tokencache.cache_path(alias)
    assert alias not in str(exc.value) or alias == ""


@pytest.mark.parametrize("alias", [None, 5, b"abc"])
def test_cache_path_rejects_non_string_alias(token_dir, alias):
    with pytest.raises(ValueError):
        tokencache.cache_path(alias)  # type: ignore[arg-type]


@pytest.mark.parametrize("fn", [
    lambda a: tokencache.load(a),
    lambda a: tokencache.save(a, _changed_cache()),
    lambda a: tokencache.alias_lock(a),
    lambda a: tokencache.locked(a).__enter__(),
])
def test_every_entry_point_validates_alias_before_touching_disk(token_dir, fn):
    with pytest.raises(ValueError):
        fn("../escape")
    assert not token_dir.exists()
    assert not (token_dir.parent / "escape_cache.bin").exists()


# ---- load -----------------------------------------------------------------------------------

def test_load_absent_returns_none(token_dir):
    assert tokencache.load("tenant-a") is None


def test_save_then_load_round_trips(token_dir):
    tokencache.save("tenant-a", _changed_cache())
    loaded = tokencache.load("tenant-a")
    assert loaded is not None
    assert _access_token_count(loaded) == 1
    assert loaded.has_state_changed is False


@pytest.mark.parametrize("content", [
    b"",
    b"   \n",
    b'{"AccessToken": {"k": {"secret": "' + SECRET.encode() + b'',  # truncated mid-write
    b"not json at all",
    b"[]",
    b"5",
    b"null",
    b'"a string"',
    b"\xff\xfe\x00garbage",
])
def test_load_corrupt_or_truncated_returns_none_without_raising(token_dir, caplog, content):
    token_dir.mkdir(parents=True)
    (token_dir / "tenant-a_cache.bin").write_bytes(content)
    with caplog.at_level(logging.DEBUG):
        assert tokencache.load("tenant-a") is None
    assert SECRET not in caplog.text


def test_load_unreadable_path_returns_none(token_dir):
    token_dir.mkdir(parents=True)
    (token_dir / "tenant-a_cache.bin").mkdir()
    assert tokencache.load("tenant-a") is None


def test_corrupt_file_can_be_overwritten_by_a_fresh_save(token_dir):
    token_dir.mkdir(parents=True)
    (token_dir / "tenant-a_cache.bin").write_text('{"AccessToken": {')
    assert tokencache.load("tenant-a") is None
    tokencache.save("tenant-a", _changed_cache())
    loaded = tokencache.load("tenant-a")
    assert loaded is not None and _access_token_count(loaded) == 1


# ---- save -----------------------------------------------------------------------------------

def test_save_creates_missing_directory(token_dir):
    assert not token_dir.exists()
    tokencache.save("tenant-a", _changed_cache())
    assert (token_dir / "tenant-a_cache.bin").is_file()


def test_save_skipped_when_state_unchanged(token_dir):
    tokencache.save("tenant-a", msal.SerializableTokenCache())
    assert not token_dir.exists()

    tokencache.save("tenant-a", _changed_cache())
    path = token_dir / "tenant-a_cache.bin"
    before = path.read_bytes()
    unchanged = tokencache.load("tenant-a")
    assert unchanged is not None
    _add_token(unchanged, 99)
    unchanged.has_state_changed = False
    tokencache.save("tenant-a", unchanged)
    assert path.read_bytes() == before


def test_save_writes_file_mode_0600_even_with_permissive_umask(token_dir):
    old = os.umask(0)
    try:
        tokencache.save("tenant-a", _changed_cache())
    finally:
        os.umask(old)
    mode = stat.S_IMODE((token_dir / "tenant-a_cache.bin").stat().st_mode)
    assert mode == 0o600


def test_save_replacing_an_existing_file_keeps_mode_0600(token_dir):
    tokencache.save("tenant-a", _changed_cache(0))
    tokencache.save("tenant-a", _changed_cache(1))
    mode = stat.S_IMODE((token_dir / "tenant-a_cache.bin").stat().st_mode)
    assert mode == 0o600


def test_save_leaves_no_temp_files_behind(token_dir):
    tokencache.save("tenant-a", _changed_cache())
    assert [p.name for p in token_dir.iterdir()] == ["tenant-a_cache.bin"]


def test_failed_replace_leaves_old_file_intact_and_no_temp_file(token_dir, monkeypatch, caplog):
    tokencache.save("tenant-a", _changed_cache(0))
    path = token_dir / "tenant-a_cache.bin"
    before = path.read_bytes()

    def boom(src, dst):
        # The temp file is fully written by now: the failure lands exactly between write and rename.
        assert Path(src).read_text()
        raise OSError("simulated crash before rename")

    replacement = _changed_cache(1)
    # A scoped patch: monkeypatch.undo() would also revert the token_dir fixture and write the retry
    # into the real tokens/ directory.
    with monkeypatch.context() as m:
        m.setattr(tokencache.os, "replace", boom)
        with caplog.at_level(logging.DEBUG), pytest.raises(OSError) as exc:
            tokencache.save("tenant-a", replacement)

    assert path.read_bytes() == before
    assert json.loads(path.read_text())
    assert [p.name for p in token_dir.iterdir()] == ["tenant-a_cache.bin"]
    assert SECRET not in str(exc.value) and SECRET not in caplog.text
    # serialize() cleared the flag; a failed save must leave the cache dirty so a retry still writes.
    assert replacement.has_state_changed is True

    tokencache.save("tenant-a", replacement)
    reloaded = tokencache.load("tenant-a")
    assert reloaded is not None and _access_token_count(reloaded) == 1
    assert path.read_bytes() != before


def test_failed_write_before_replace_leaves_old_file_intact(token_dir, monkeypatch):
    tokencache.save("tenant-a", _changed_cache(0))
    path = token_dir / "tenant-a_cache.bin"
    before = path.read_bytes()

    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr(tokencache.os, "fsync", boom)
    with pytest.raises(OSError):
        tokencache.save("tenant-a", _changed_cache(1))
    assert path.read_bytes() == before
    assert [p.name for p in token_dir.iterdir()] == ["tenant-a_cache.bin"]


# ---- locking --------------------------------------------------------------------------------

def test_alias_lock_is_one_object_per_alias(token_dir):
    assert tokencache.alias_lock("a") is tokencache.alias_lock("a")
    assert tokencache.alias_lock("a") is not tokencache.alias_lock("b")


def test_alias_lock_creation_is_race_free(token_dir):
    seen: list = []
    barrier = threading.Barrier(16)

    def grab():
        barrier.wait()
        seen.append(tokencache.alias_lock("racy"))

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(seen) == 16 and len({id(lock) for lock in seen}) == 1


def test_same_alias_serializes_and_different_alias_does_not_block(token_dir):
    holder_in = threading.Event()
    release = threading.Event()
    same_entered = threading.Event()
    other_entered = threading.Event()

    def holder():
        with tokencache.locked("shared"):
            holder_in.set()
            release.wait(timeout=10)

    def same():
        with tokencache.locked("shared"):
            same_entered.set()

    def other():
        with tokencache.locked("different"):
            other_entered.set()

    t_holder = threading.Thread(target=holder)
    t_holder.start()
    assert holder_in.wait(timeout=5)

    t_same = threading.Thread(target=same)
    t_other = threading.Thread(target=other)
    t_same.start()
    t_other.start()

    assert other_entered.wait(timeout=5), "a different alias must not wait on the held lock"
    assert not same_entered.wait(timeout=0.3), "the same alias must wait for the holder"

    release.set()
    assert same_entered.wait(timeout=5)
    for t in (t_holder, t_same, t_other):
        t.join(timeout=5)
        assert not t.is_alive()


def test_locked_releases_on_exception(token_dir):
    with pytest.raises(RuntimeError):
        with tokencache.locked("tenant-a"):
            raise RuntimeError("boom")
    assert tokencache.alias_lock("tenant-a").acquire(timeout=1)
    tokencache.alias_lock("tenant-a").release()


def test_concurrent_read_modify_write_loses_no_update(token_dir):
    workers = 12
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def work(n: int):
        try:
            barrier.wait()
            with tokencache.locked("tenant-a"):
                cache = tokencache.load("tenant-a") or msal.SerializableTokenCache()
                _add_token(cache, n)
                tokencache.save("tenant-a", cache)
        except BaseException as exc:  # surfaced through the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    final = tokencache.load("tenant-a")
    assert final is not None
    assert _access_token_count(final) == workers


# ---- token_dir override ------------------------------------------------------------------------------


@pytest.fixture
def other_dir(tmp_path):
    return tmp_path / "elsewhere"


def test_cache_path_token_dir_overrides_module_default(token_dir, other_dir):
    assert tokencache.cache_path("tenant-a", token_dir=other_dir) == other_dir / "tenant-a_cache.bin"
    assert tokencache.cache_path("tenant-a", token_dir=None) == token_dir / "tenant-a_cache.bin"


def test_save_and_load_with_token_dir_never_touch_the_module_default(token_dir, other_dir):
    tokencache.save("tenant-a", _changed_cache(), token_dir=other_dir)

    assert (other_dir / "tenant-a_cache.bin").exists()
    assert not token_dir.exists()
    assert tokencache.load("tenant-a") is None
    loaded = tokencache.load("tenant-a", token_dir=other_dir)
    assert loaded is not None and _access_token_count(loaded) == 1


def test_locked_with_token_dir_holds_the_alias_lock_and_validates_the_alias(token_dir, other_dir):
    with tokencache.locked("tenant-a", token_dir=other_dir):
        assert tokencache.alias_lock("tenant-a").locked()
    assert not tokencache.alias_lock("tenant-a").locked()
    with pytest.raises(ValueError):
        tokencache.locked("../evil", token_dir=other_dir).__enter__()


@pytest.mark.parametrize("fn", [
    lambda d, a: tokencache.cache_path(a, token_dir=d),
    lambda d, a: tokencache.load(a, token_dir=d),
    lambda d, a: tokencache.save(a, _changed_cache(), token_dir=d),
])
def test_token_dir_does_not_bypass_alias_validation(token_dir, other_dir, fn):
    with pytest.raises(ValueError):
        fn(other_dir, "../evil")
    assert not other_dir.exists()
