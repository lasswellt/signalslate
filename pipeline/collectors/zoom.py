"""
Zoom collector: yesterday's meetings, merged from three endpoints, with AI Companion summaries
and participants for meetings this connection hosted.

A meeting can show up in any of three places, and none of them is complete on its own:

- GET /users/me/meeting_summaries — host-only, and the only source with a generated summary
  attached. Enumerate on summary_created_time, then fetch each body with
  GET /meetings/{meetingUUID}/meeting_summary. The /accounts/... variant is a master-account
  endpoint and 403s on a single Pro/Business account.
- GET /users/me/meetings?type=previous_meetings — scheduled meetings whose end time has passed.
  Does not return instant meetings, so it catches meetings a summary-only view would miss.
- GET /report/users/{userId}/meetings?type=pastJoined — S2S only (report:read:user:admin); every
  past meeting the account's user hosted OR joined. Needs a real user id: reports may reject "me".

Meetings are merged by uuid across whichever of these were reachable this run. A meeting is
"hosted" when it came from the summaries list (host-only by construction) or its host id/email
matches the signed-in user (GET /users/me, resolved once per run). Participants
(GET /past_meetings/{uuid}/participants) and the summary body are fetched only for hosted
meetings — Zoom doesn't expose either to a mere participant.

Two behaviours worth knowing, both from docs/_research/2026-09-13_auth-approach.md:

- Summaries are generated *after* a meeting ends, sometimes well after, and sometimes never
  ("insufficient transcript"). So the lookback is deliberately wider than the digest window, and a
  meeting with no summary is recorded with summary_available=False rather than dropped — the
  digest can say "no summary available" instead of silently under-reporting the day.
- A meeting UUID starting with "/" or containing "//" must be double URL-encoded.

Both endpoints are carried forward from the auth research and have not been exercised against a
live account. Verify empirically before trusting this collector.
"""
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import requests

from pipeline.collectors import CollectionResult, Item, parse_iso
from pipeline.health import ZoomAuthError, zoom_mode, zoom_token

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


def _paged(token: str, path: str, params: dict, list_key: str) -> list[dict]:
    """GET path, following next_page_token up to MAX_PAGES, collecting body[list_key]."""
    out: list[dict] = []
    next_token: Optional[str] = None
    for _ in range(MAX_PAGES):
        page_params = {**params, **({"next_page_token": next_token} if next_token else {})}
        body = _get(token, path, page_params)
        out.extend(body.get(list_key, []))
        next_token = body.get("next_page_token") or None
        if not next_token:
            break
    return out


def list_summaries(token: str, since: datetime, until: datetime) -> list[dict]:
    params = {
        # OpenAPI spec: yyyy-MM-dd'T'HH:mm:ss'Z' UTC, not date-only — a date-only `from`/`to` is
        # silently reinterpreted and can shift the window by up to a day.
        "from": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_filter_field": "summary_created_time",
        "page_size": PAGE_SIZE,
    }
    return _paged(token, "/users/me/meeting_summaries", params, "summaries")


def _me(token: str) -> dict:
    """{"id": str|None, "email": str|None} for the signed-in user. Resolved once per run: used to
    decide which merged meetings this connection hosted, and (s2s) as the report's user id."""
    body = _get(token, "/users/me")
    return {"id": body.get("id"), "email": body.get("email")}


def _list_previous(token: str, since: datetime, until: datetime) -> list[dict]:
    """previous_meetings: scheduled meetings whose end time has passed. No instant meetings."""
    params = {
        "type": "previous_meetings",
        # date-only per the OpenAPI spec for this endpoint, unlike list_summaries above.
        "from": since.strftime("%Y-%m-%d"),
        "to": until.strftime("%Y-%m-%d"),
        "page_size": PAGE_SIZE,
    }
    return _paged(token, "/users/me/meetings", params, "meetings")


def _list_joined_report(token: str, user_id: str, since: datetime, until: datetime) -> list[dict]:
    """pastJoined report: every past meeting the user hosted or joined. S2S only; needs a real
    user id — the report endpoint may reject the "me" alias other user-scoped endpoints accept."""
    params = {
        "type": "pastJoined",
        "from": since.strftime("%Y-%m-%d"),
        "to": until.strftime("%Y-%m-%d"),
        "page_size": PAGE_SIZE,
    }
    return _paged(token, f"/report/users/{user_id}/meetings", params, "meetings")


def _participants(token: str, uuid: str) -> list[dict]:
    """Deduped {"name", "email"} pairs for a hosted meeting. 404 (no participant data) is the
    caller's problem to interpret — this only fetches and dedupes what came back."""
    raw = _paged(
        token, f"/past_meetings/{encode_uuid(uuid)}/participants", {"page_size": PAGE_SIZE}, "participants"
    )
    seen: set[tuple] = set()
    out: list[dict] = []
    for p in raw:
        key = (p.get("name"), p.get("user_email"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": p.get("name"), "email": p.get("user_email")})
    return out


def _hosted(entry: dict, from_summary: bool, me: dict) -> bool:
    """True when the meeting is host-only by construction (came from the summaries list) or its
    host id/email matches the signed-in user."""
    if from_summary:
        return True
    host_id = entry.get("host_id")
    if host_id and me.get("id") and host_id == me["id"]:
        return True
    host_email = entry.get("host_email")
    if host_email and me.get("email") and host_email.lower() == me["email"].lower():
        return True
    return False


def _merge(summaries: list[dict], previous: list[dict], joined: list[dict]) -> tuple[dict[str, dict], set[str]]:
    """Union the three sources by meeting uuid. Returns (uuid -> merged fields, uuids that came
    from the summaries list — those are host-only by construction, so always "hosted")."""
    meetings: dict[str, dict] = {}
    from_summary: set[str] = set()

    def touch(uuid: str) -> dict:
        return meetings.setdefault(uuid, {
            "topic": None, "start_time": None, "end_time": None, "host_id": None, "host_email": None,
        })

    for s in summaries:
        uuid = s.get("meeting_uuid") or s.get("meeting_id")
        if not uuid:
            continue
        uuid = str(uuid)
        entry = touch(uuid)
        entry["topic"] = entry["topic"] or s.get("meeting_topic")
        entry["start_time"] = entry["start_time"] or s.get("meeting_start_time")
        entry["end_time"] = entry["end_time"] or s.get("meeting_end_time")
        entry["host_id"] = entry["host_id"] or s.get("meeting_host_id")
        entry["host_email"] = entry["host_email"] or s.get("meeting_host_email")
        from_summary.add(uuid)

    for p in previous:
        uuid = p.get("uuid") or p.get("id")
        if not uuid:
            continue
        uuid = str(uuid)
        entry = touch(uuid)
        entry["topic"] = entry["topic"] or p.get("topic")
        entry["start_time"] = entry["start_time"] or p.get("start_time")
        entry["host_id"] = entry["host_id"] or p.get("host_id")

    for j in joined:
        uuid = j.get("uuid") or j.get("id")
        if not uuid:
            continue
        uuid = str(uuid)
        entry = touch(uuid)
        entry["topic"] = entry["topic"] or j.get("topic")
        entry["start_time"] = entry["start_time"] or j.get("start_time")
        entry["end_time"] = entry["end_time"] or j.get("end_time")
        entry["host_id"] = entry["host_id"] or j.get("host_id")
        entry["host_email"] = entry["host_email"] or j.get("user_email")

    return meetings, from_summary


def collect_zoom(since: datetime, until: datetime) -> CollectionResult:
    try:
        token = zoom_token()
    except ZoomAuthError:
        return CollectionResult(
            "zoom", "error", "Zoom sign-in expired or missing — sign in again on the Connections page"
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as an error row, never raised at the run
        return CollectionResult("zoom", "error", f"token fetch failed: {exc}")

    try:
        me = _me(token)
    except ZoomError as exc:
        return CollectionResult("zoom", "error", f"could not resolve signed-in user: {exc}")

    mode = zoom_mode()
    # At least SUMMARY_LOOKBACK back so late-generated summaries are caught, but honour an earlier
    # `since` when the cursor says a longer window is outstanding — otherwise an outage longer than
    # 48h loses those meetings permanently, because the cursor still advances to now.
    lookback_start = min(since, until - SUMMARY_LOOKBACK)

    sources_failed: list[str] = []
    sources_attempted = 0

    summaries: list[dict] = []
    sources_attempted += 1
    try:
        summaries = list_summaries(token, lookback_start, until)
    except ZoomError as exc:
        sources_failed.append(f"summaries: {exc}")

    previous: list[dict] = []
    sources_attempted += 1
    try:
        previous = _list_previous(token, lookback_start, until)
    except ZoomError as exc:
        sources_failed.append(f"previous meetings: {exc}")

    joined: list[dict] = []
    if mode == "s2s":
        sources_attempted += 1
        try:
            joined = _list_joined_report(token, me.get("id") or "me", lookback_start, until)
        except ZoomError as exc:
            sources_failed.append(f"report: {exc}")

    if sources_failed and len(sources_failed) == sources_attempted:
        return CollectionResult("zoom", "error", "; ".join(sources_failed))

    merged, from_summary = _merge(summaries, previous, joined)

    items: list[Item] = []
    missing = 0
    hosted_count = 0
    failures: list[str] = list(sources_failed)

    for uuid, entry in merged.items():
        hosted = _hosted(entry, uuid in from_summary, me)
        occurred = (
            parse_iso(entry.get("start_time") or "")
            or parse_iso(entry.get("end_time") or "")
            or until
        )

        payload = {
            "topic": entry.get("topic"),
            "start_time": entry.get("start_time"),
            "end_time": entry.get("end_time"),
            "host_email": entry.get("host_email"),
            "hosted": hosted,
        }

        if not hosted:
            payload["participants"] = []
            payload["summary_available"] = False
            payload["summary_unavailable_reason"] = "not_host"
            items.append(Item("meeting", uuid, occurred, payload))
            continue

        hosted_count += 1
        summary_body = None
        if uuid in from_summary:
            try:
                summary_body = _get(token, f"/meetings/{encode_uuid(uuid)}/meeting_summary")
            except ZoomError as exc:
                if str(exc) == "404":
                    # Meeting happened, summary never generated. Record it as such.
                    missing += 1
                else:
                    failures.append(f"{uuid} summary: {exc}")
        else:
            # Hosted, but never appeared in the summaries list — no summary was ever generated.
            missing += 1

        payload["summary_content"] = (summary_body or {}).get("summary_content")
        payload["summary_doc_url"] = (summary_body or {}).get("summary_doc_url")
        payload["summary_available"] = summary_body is not None

        try:
            payload["participants"] = _participants(token, uuid)
        except ZoomError as exc:
            payload["participants"] = []
            if str(exc) != "404":
                failures.append(f"{uuid} participants: {exc}")

        items.append(Item("meeting", uuid, occurred, payload))

    detail = f"{len(items)} meetings ({hosted_count} hosted)"
    if missing:
        detail += f", {missing} without a summary"
    if failures:
        return CollectionResult("zoom", "partial", f"{detail} ({len(failures)} failed: {failures[0]})", items)
    return CollectionResult("zoom", "ok", detail, items)
