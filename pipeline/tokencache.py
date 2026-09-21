"""
The one place an MSAL token cache is read from and written to disk.

Why this exists (docs/_research/2026-09-21_management-ui.md, codebase finding Q3): the health check,
the Graph collector and the sign-in flow each did their own read-modify-write of
tokens/<alias>_cache.bin. That had three faults. Two callers refreshing the same alias at once lose
one another's update (MSAL rotates the refresh token on use, so losing the winner's write strands the
next refresh). write_text truncates first, so a crash mid-write leaves a partial file. And a partial
file made deserialize raise out of check_all_configured, failing every source in the run.

Design decisions:
- One threading.Lock per alias, created under a module lock. Callers hold it across the whole
  load -> acquire_token -> save sequence via locked(); load() and save() never take it themselves
  because the lock is not reentrant.
- Writes go to a temp file in the same directory (same filesystem, so os.replace is atomic), created
  0600 and fsynced before the replace. A reader sees the old file or the new one, never a torn one.
- A corrupt, truncated or unreadable file loads as None, the same as an absent one. The callers already
  turn "no cache" into a per-source "re-run the sign-in" error, which is the right degradation.
- The alias becomes part of a filename, so it is validated before any path is built (path traversal
  guard). The rule is a safe FILENAME class, ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$, not the stricter
  [A-Za-z0-9-]+ that new UI-created connections must satisfy: installs that predate the UI have
  aliases such as acme_corp or acme.com in .env and a matching <alias>_cache.bin on disk, and
  narrowing this rule made check_all_configured raise for them even with no key configured. No
  separator, no leading dot and a length cap keep every alias inside TOKEN_DIR.

Stdlib plus msal only: this module must stay importable from pipeline.health without a cycle.
"""
import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import msal

ROOT = Path(__file__).resolve().parent.parent
# Module attribute, read at call time, so tests can point it at a tmp_path.
TOKEN_DIR = ROOT / "tokens"

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_FILE_MODE = 0o600

_log = logging.getLogger(__name__)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def cache_path(alias: str, *, token_dir: Optional[Path] = None) -> Path:
    """
    Path of the cache file for one alias.

    token_dir defaults to the module TOKEN_DIR, read at call time. A caller that owns its own
    directory setting (pipeline.health.TOKEN_DIR, which tests and deployments repoint) passes it
    explicitly; otherwise its cache would be read from one directory and written to another.

    Raises ValueError for any alias outside [A-Za-z0-9][A-Za-z0-9_.-]{0,63} (empty, separators, a
    leading dot, whitespace, NUL, over 64 characters): the alias is interpolated into a filename, so
    anything else could escape TOKEN_DIR.
    """
    # fullmatch, not $: "$" also matches before a trailing newline, which would let "a\n" through.
    if not isinstance(alias, str) or _ALIAS_RE.fullmatch(alias) is None:
        raise ValueError("invalid alias: must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    return (TOKEN_DIR if token_dir is None else token_dir) / f"{alias}_cache.bin"


def alias_lock(alias: str) -> threading.Lock:
    """
    The lock serializing cache access for one alias; the same object on every call for that alias.

    Raises ValueError for an invalid alias so a bad name never allocates a lock entry.
    """
    cache_path(alias)
    with _locks_guard:
        lock = _locks.get(alias)
        if lock is None:
            lock = _locks[alias] = threading.Lock()
        return lock


@contextmanager
def locked(alias: str, *, token_dir: Optional[Path] = None) -> Iterator[None]:
    """
    Hold the alias lock around a load-modify-save sequence.

    token_dir only lets a caller pass the same arguments to locked, load and save. The lock stays
    per alias, not per file: the same alias in two directories over-serializes, which is harmless.
    """
    cache_path(alias, token_dir=token_dir)
    with alias_lock(alias):
        yield


def load(alias: str, *, token_dir: Optional[Path] = None) -> Optional[msal.SerializableTokenCache]:
    """
    The alias's cache, or None when there is no usable one.

    None covers an absent file and also an empty, truncated, non-JSON, non-object or unreadable one:
    a damaged cache must degrade to "sign in again", never raise out of a run. The returned cache has
    has_state_changed False, so save() only writes after MSAL actually modified it.

    Raises ValueError only for an invalid alias.
    """
    path = cache_path(alias, token_dir=token_dir)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        _log.warning("token cache for alias %r is unreadable; treating as absent", alias)
        return None

    try:
        # MSAL's deserialize accepts any JSON value and stores it as-is, so a valid-but-wrong
        # document ("[]", "5") would load fine and fail later inside MSAL. Check the shape here.
        if not text.strip() or not isinstance(json.loads(text), dict):
            raise ValueError("not a JSON object")
        cache = msal.SerializableTokenCache()
        cache.deserialize(text)
    except ValueError:
        _log.warning("token cache for alias %r is corrupt; treating as absent", alias)
        return None
    return cache


def save(alias: str, cache: msal.SerializableTokenCache, *, token_dir: Optional[Path] = None) -> None:
    """
    Persist the cache atomically, and only if MSAL changed it since it was loaded.

    The temp file lives in the target directory so os.replace is a same-filesystem rename, and it is
    removed if anything fails before the replace, leaving the previous file untouched.

    Raises ValueError for an invalid alias; OSError if the write or replace fails.
    """
    path = cache_path(alias, token_dir=token_dir)
    if not cache.has_state_changed:
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{alias}_cache.", suffix=".tmp")
    try:
        # mkstemp already creates 0600; fchmod keeps that true even if a platform default differs.
        os.fchmod(fd, _FILE_MODE)
        fh = os.fdopen(fd, "w")
    except BaseException:
        os.close(fd)
        _discard(tmp_name)
        raise
    try:
        with fh:
            fh.write(cache.serialize())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        _discard(tmp_name)
        # serialize() cleared the flag; a failed write must not make the next save() a no-op.
        cache.has_state_changed = True
        raise


def _discard(tmp_name: str) -> None:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass
