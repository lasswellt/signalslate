"""
Zoom collector: yesterday's meetings and their AI Companion summaries.

Enumerate with GET /users/{userId}/meeting_summaries filtered on summary_created_time, then fetch
each body with GET /meetings/{meetingUUID}/meeting_summary. The /accounts/... variant is a
master-account endpoint and 403s on a single Pro/Business account.

Two behaviours worth knowing, both from docs/_research/2026-09-13_auth-approach.md:

- Summaries are generated *after* a meeting ends, sometimes well after, and sometimes never
  ("insufficient transcript"). So the lookback is deliberately wider than the digest window, and a
  meeting with no summary is recorded with summary=None rather than dropped — the digest can say
  "no summary available" instead of silently under-reporting the day.
- A meeting UUID starting with "/" or containing "//" must be double URL-encoded.

Both endpoints are carried forward from the auth research and have not been exercised against a
live account. Verify empirically before trusting this collector.
"""
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import requests

from pipeline.collectors import CollectionResult, Item, parse_iso
from pipeline.health import zoom_token

ZOOM = "https://api.zoom.us/v2"
TIMEOUT = 15
PAGE_SIZE = 300
MAX_PAGES = 10
# Wider than the digest window: a meeting that ended late yesterday may only have had its summary
# generated this morning.
SUMMARY_LOOKBACK = timedelta(hours=48)


class ZoomError(RuntimeError):
    pass


def encode_uuid(uuid: str) -> str:
    """
    Double-encode a UUID that starts with "/" or contains "//", single-encode otherwise.

    Zoom's own rule. Getting it wrong routes the request to a different (or nonexistent) path.
    """
    once = quote(uuid, safe="")
    if uuid.startswith("/") or "//" in uuid:
        return quote(once, safe="")
    return once


def _get(token: str, path: str, params: Optional[dict] = None) -> dict:
    try:
        resp = requests.get(
            f"{ZOOM}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ZoomError(str(exc)) from exc

    if resp.status_code == 429:
        raise ZoomError(f"rate limited, Retry-After={resp.headers.get('Retry-After', '?')}")
    if resp.status_code == 404:
        raise ZoomError("404")
    if resp.status_code >= 400:
        raise ZoomError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ZoomError("non-JSON response") from exc


def list_summaries(token: str, since: datetime, until: datetime) -> list[dict]:
    out: list[dict] = []
    params = {
        # OpenAPI spec: yyyy-MM-dd'T'HH:mm:ss'Z' UTC, not date-only — a date-only `from`/`to` is
        # silently reinterpreted and can shift the window by up to a day.
        "from": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_filter_field": "summary_created_time",
        "page_size": PAGE_SIZE,
    }
    token_param: Optional[str] = None

    for _ in range(MAX_PAGES):
        body = _get(token, "/users/me/meeting_summaries", {**params, **({"next_page_token": token_param} if token_param else {})})
        out.extend(body.get("summaries", []))
        token_param = body.get("next_page_token") or None
        if not token_param:
            break
    return out


def collect_zoom(since: datetime, until: datetime) -> CollectionResult:
    try:
        token = zoom_token()
    except Exception as exc:  # noqa: BLE001 — surfaced as an error row, never raised at the run
        return CollectionResult("zoom", "error", f"token fetch failed: {exc}")

    # At least SUMMARY_LOOKBACK back so late-generated summaries are caught, but honour an earlier
    # `since` when the cursor says a longer window is outstanding — otherwise an outage longer than
    # 48h loses those meetings permanently, because the cursor still advances to now.
    lookback_start = min(since, until - SUMMARY_LOOKBACK)

    try:
        summaries = list_summaries(token, lookback_start, until)
    except ZoomError as exc:
        return CollectionResult("zoom", "error", str(exc))

    items: list[Item] = []
    missing = 0
    failures: list[str] = []

    for summary in summaries:
        uuid = summary.get("meeting_uuid") or summary.get("meeting_id")
        if not uuid:
            continue
        occurred = (
            parse_iso(summary.get("summary_start_time", ""))
            or parse_iso(summary.get("summary_created_time", ""))
            or until
        )

        body = None
        try:
            body = _get(token, f"/meetings/{encode_uuid(str(uuid))}/meeting_summary")
        except ZoomError as exc:
            if str(exc) == "404":
                # Meeting happened, summary never generated. Record it as such.
                missing += 1
            else:
                failures.append(f"{uuid}: {exc}")
                continue

        payload = {
            **summary,
            # summary_overview / summary_details / next_steps are deprecated; summary_content is
            # the unified Markdown body and the only field worth reading.
            "summary_content": (body or {}).get("summary_content"),
            "summary_available": body is not None,
        }
        items.append(Item("meeting", str(uuid), occurred, payload))

    detail = f"{len(items)} meetings"
    if missing:
        detail += f", {missing} without a summary"
    if failures:
        return CollectionResult("zoom", "partial", f"{detail} ({len(failures)} failed: {failures[0]})", items)
    return CollectionResult("zoom", "ok", detail, items)
