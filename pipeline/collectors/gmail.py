"""
Gmail transport: list message ids for a window, with quota-aware retry.

Design decisions (docs/_research/2026-09-20_gmail-collector-and-agents.md §4, §5):
- The window goes to messages.list as epoch SECONDS. Date strings in `q` are read as midnight PST,
  so a UTC window expressed that way is silently shifted by hours.
- No batch, no history.list, no watch. Batch has the Graph envelope problem (outer 200, per-part
  failures) at an identical quota cost; history.list and push watches need a persisted cursor and
  a full-sync fallback. A timestamp read self-heals, those don't.
- No parallel calls. Gmail returns 429 for a per-user concurrent-request limit, so requests go
  strictly one at a time and lean on backoff instead of bursting.
"""
import random
import time
from datetime import datetime, timezone
from typing import Optional

import requests

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT = 15
# Documented maximum for messages.list; anything smaller just costs more 5-unit calls.
PAGE_SIZE = 500
# 40 x 500 = 20k messages in one window. Past that something is wrong (a backfill after a long
# outage) and a truncated list would under-report exactly when it matters.
MAX_PAGES = 40
# Total requests per call. Backoff sleeps 1, 2, 4, 8, 16, 32s between them (~1 minute), which is
# the length of the per-user quota window that a rate-limit 403 is waiting out.
MAX_ATTEMPTS = 7
MAX_BACKOFF = 32
RETRY_STATUS = {429, 500, 502, 503, 504}
# Compared after lowercasing and dropping underscores, so RATE_LIMIT_EXCEEDED matches too.
RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded"}

# Indirections so tests neither wait nor depend on real randomness.
_sleep = time.sleep
_random = random.random


class GmailError(RuntimeError):
    """A Gmail call failed in a way the caller should report, not retry."""


def to_epoch_seconds(moment: datetime) -> int:
    """
    Naive-UTC datetime -> epoch seconds.

    The explicit tzinfo is the point: a naive .timestamp() reads the value as LOCAL time, which under
    any non-UTC container TZ shifts the window by the UTC offset.
    """
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def _backoff(attempt: int) -> float:
    """Truncated exponential backoff: min(2^n + random fraction of a second, MAX_BACKOFF)."""
    return min(2**attempt + _random(), MAX_BACKOFF)


def _error_reasons(resp: requests.Response) -> set[str]:
    """
    Every reason-like string in a Gmail error body, normalised.

    Gmail documents errors[].reason, but Google's newer error shape puts the same information in
    error.status and error.details[].reason, so all three are read.
    """
    try:
        body = resp.json()
    except ValueError:
        return set()
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return set()

    found = [error.get("status")]
    for group in ("errors", "details"):
        entries = error.get(group)
        if isinstance(entries, list):
            found.extend(e.get("reason") for e in entries if isinstance(e, dict))
    return {str(r).lower().replace("_", "") for r in found if r}


def _retryable(resp: requests.Response) -> bool:
    if resp.status_code in RETRY_STATUS:
        return True
    # Rate limits arrive as 403, not only 429. Any OTHER 403 (insufficientPermissions, a revoked
    # scope) is permanent, and retrying it just burns a minute before failing anyway.
    return resp.status_code == 403 and bool(_error_reasons(resp) & RATE_REASONS)


def _call(token: str, path: str, params: Optional[dict] = None) -> dict:
    """GET one Gmail resource; `path` is relative to users/me, e.g. "/messages"."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GMAIL}{path}"

    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise GmailError(f"{path}: {exc}") from exc

        if resp.status_code < 400:
            try:
                return resp.json()
            except ValueError as exc:
                raise GmailError(f"{path}: non-JSON response") from exc

        if not _retryable(resp):
            raise GmailError(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}")
        if attempt < MAX_ATTEMPTS - 1:
            _sleep(_backoff(attempt))

    raise GmailError(f"{path}: still failing after {MAX_ATTEMPTS} attempts (last HTTP {resp.status_code})")


def list_message_ids(token: str, since: datetime, until: datetime) -> list[dict]:
    """
    Every {id, threadId} Gmail lists for [since, until), all pages drained.

    The after:/before: boundary inclusivity and list ordering are undocumented, so callers overlap the
    window, window again on internalDate, and dedupe by id.
    """
    params = {
        "q": f"after:{to_epoch_seconds(since)} before:{to_epoch_seconds(until)}",
        "maxResults": PAGE_SIZE,
    }
    out: list[dict] = []
    page_token: Optional[str] = None

    for _ in range(MAX_PAGES):
        page_params = {**params, "pageToken": page_token} if page_token else dict(params)
        body = _call(token, "/messages", page_params)
        out.extend(body.get("messages") or [])
        page_token = body.get("nextPageToken")
        if not page_token:
            return out

    raise GmailError(f"more than {MAX_PAGES} pages — window too large, results truncated")
