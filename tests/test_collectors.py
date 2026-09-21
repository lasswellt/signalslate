"""
Collector tests. No network: requests is stubbed at each collector module's own import site,
following tests/test_health.py's monkeypatch-the-module style.

The traps these pin, all from docs/_research/2026-09-13_phase2-collectors.md:
  - Graph ignores $filter unless $orderby names the same property (silent, returns everything).
  - Slack's non-Marketplace cap returns exactly 15 objects regardless of the requested limit.
  - Zoom UUIDs containing "/" must be double-encoded.
"""
import base64
import copy
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.collectors import Item, parse_iso, parse_slack_ts, to_graph_time  # noqa: E402
from pipeline.collectors import gmail, graph, slack, zoom  # noqa: E402
from pipeline import health  # noqa: E402

SINCE = datetime(2026, 9, 12, 6, 0, 0)
UNTIL = datetime(2026, 9, 13, 6, 0, 0)


class FakeResponse:
    def __init__(self, json_data, status_code=200, headers=None, text=""):
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def route(monkeypatch, module, handler):
    """Stub module.requests.get with a handler(url, params) -> FakeResponse."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "params": params or {}})
        return handler(url, params or {})

    monkeypatch.setattr(module.requests, "get", fake_get)
    return calls


# --- shared parsing -----------------------------------------------------------------


def test_parse_iso_returns_naive_utc():
    parsed = parse_iso("2026-09-13T20:48:29.832Z")
    assert parsed == datetime(2026, 9, 13, 20, 48, 29, 832000)
    assert parsed.tzinfo is None


def test_parse_iso_converts_offset_to_utc():
    assert parse_iso("2026-09-13T16:48:29-04:00") == datetime(2026, 9, 13, 20, 48, 29)


def test_parse_iso_bad_value_is_none_not_raise():
    assert parse_iso("not a date") is None
    assert parse_iso("") is None


def test_parse_slack_ts():
    parsed = parse_slack_ts("1773468000.000100")
    assert parsed is not None and parsed.tzinfo is None


def test_parse_slack_ts_bad_value():
    assert parse_slack_ts("nope") is None


def test_to_graph_time_format():
    assert to_graph_time(datetime(2026, 9, 13, 6, 0, 0)) == "2026-09-13T06:00:00Z"


# --- Graph --------------------------------------------------------------------------


def test_mail_filter_and_orderby_agree(monkeypatch):
    """Graph silently ignores $filter unless $orderby names the same property."""
    calls = route(monkeypatch, graph, lambda url, params: FakeResponse({"value": []}))
    graph.collect_mail("tok", SINCE, UNTIL)

    params = calls[0]["params"]
    assert "receivedDateTime ge 2026-09-12T06:00:00Z" in params["$filter"]
    assert params["$orderby"].startswith("receivedDateTime")


def test_chat_filter_and_orderby_agree(monkeypatch):
    """The silent-failure trap: same property in both, or the whole chat history comes back."""
    def handler(url, params):
        if url.endswith("/me/chats"):
            return FakeResponse({"value": [{"id": "19:abc", "chatType": "oneOnOne"}]})
        return FakeResponse({"value": []})

    calls = route(monkeypatch, graph, handler)
    graph.collect_chat("tok", SINCE, UNTIL)

    message_call = [c for c in calls if "/messages" in c["url"]][0]
    assert "lastModifiedDateTime" in message_call["params"]["$filter"]
    assert message_call["params"]["$orderby"].startswith("lastModifiedDateTime")


def test_chat_rejects_messages_outside_the_window(monkeypatch):
    """Defence in depth: if the filter were ignored, out-of-window messages must still be dropped."""
    outside = {
        "id": "m-old",
        "messageType": "message",
        "lastModifiedDateTime": "2020-01-01T00:00:00Z",
    }
    inside = {
        "id": "m-new",
        "messageType": "message",
        "lastModifiedDateTime": "2026-09-12T12:00:00Z",
    }

    def handler(url, params):
        if url.endswith("/me/chats"):
            return FakeResponse({"value": [{"id": "19:abc"}]})
        return FakeResponse({"value": [outside, inside]})

    route(monkeypatch, graph, handler)
    items = graph.collect_chat("tok", SINCE, UNTIL)
    assert [i.external_id for i in items] == ["19:abc:m-new"]


def test_chat_drops_system_event_messages(monkeypatch):
    def handler(url, params):
        if url.endswith("/me/chats"):
            return FakeResponse({"value": [{"id": "19:abc"}]})
        return FakeResponse({"value": [
            {"id": "sys", "messageType": "systemEventMessage", "lastModifiedDateTime": "2026-09-12T12:00:00Z"},
            {"id": "real", "messageType": "message", "lastModifiedDateTime": "2026-09-12T12:00:00Z"},
        ]})

    route(monkeypatch, graph, handler)
    assert [i.external_id for i in graph.collect_chat("tok", SINCE, UNTIL)] == ["19:abc:real"]


def test_pagination_follows_nextlink_without_resending_params(monkeypatch):
    """nextLink already carries the query; re-sending params is how you lose the filter."""
    def handler(url, params):
        if "page2" in url:
            return FakeResponse({"value": [{"id": "b"}]})
        return FakeResponse({"value": [{"id": "a"}], "@odata.nextLink": "https://graph/page2"})

    calls = route(monkeypatch, graph, handler)
    pages = list(graph.get_pages("tok", "https://graph/start", {"$top": 50}))

    assert len(pages) == 2
    assert calls[0]["params"] == {"$top": 50}
    assert calls[1]["params"] == {}  # second call sends none


def test_pagination_raises_rather_than_truncating_silently(monkeypatch):
    """Under-reporting without a signal is worst during a post-outage backfill."""
    route(monkeypatch, graph, lambda url, params: FakeResponse(
        {"value": [{"id": "x"}], "@odata.nextLink": "https://graph/next"}
    ))
    with pytest.raises(graph.GraphError, match="truncated"):
        list(graph.get_pages("tok", "https://graph/start"))


def test_pagination_does_not_raise_when_it_finishes(monkeypatch):
    route(monkeypatch, graph, lambda url, params: FakeResponse({"value": [{"id": "x"}]}))
    assert len(list(graph.get_pages("tok", "https://graph/start"))) == 1


def test_chat_list_requests_expand_for_the_preview(monkeypatch):
    """
    Ordering by lastMessagePreview does not return it — only $expand does.

    Without this the cutoff below reads None on every chat and never fires, so the fan-out it
    exists to prevent happens anyway, invisibly.
    """
    calls = route(monkeypatch, graph, lambda url, params: FakeResponse({"value": []}))
    graph.collect_chat("tok", SINCE, UNTIL)
    assert calls[0]["params"]["$expand"] == "lastMessagePreview"


def test_chat_ids_are_scoped_to_their_chat(monkeypatch):
    """chatMessage.id is unique only within its chat; two chats can produce the same id."""
    def handler(url, params):
        if url.endswith("/me/chats"):
            return FakeResponse({"value": [{"id": "chatA"}, {"id": "chatB"}]})
        return FakeResponse({"value": [
            {"id": "1622853091207", "messageType": "message", "lastModifiedDateTime": "2026-09-12T12:00:00Z"},
        ]})

    route(monkeypatch, graph, handler)
    ids = [i.external_id for i in graph.collect_chat("tok", SINCE, UNTIL)]
    assert ids == ["chatA:1622853091207", "chatB:1622853091207"]
    assert len(set(ids)) == 2  # would collapse to 1 unscoped


def test_chat_cutoff_allows_a_grace_period_for_edits(monkeypatch):
    """A chat whose last message is old but was edited in-window must still be scanned."""
    edited_recently = {
        "id": "m1", "messageType": "message", "lastModifiedDateTime": "2026-09-12T12:00:00Z",
    }

    def handler(url, params):
        if url.endswith("/me/chats"):
            # Created 3 days before the window — inside CHAT_SCAN_GRACE, so not skipped.
            return FakeResponse({"value": [
                {"id": "old-but-edited", "lastMessagePreview": {"createdDateTime": "2026-09-09T12:00:00Z"}},
            ]})
        return FakeResponse({"value": [edited_recently]})

    route(monkeypatch, graph, handler)
    assert len(graph.collect_chat("tok", SINCE, UNTIL)) == 1


def test_chat_enumeration_stops_at_first_stale_chat(monkeypatch):
    """Newest-first ordering means one chat past the cutoff implies every later one is too."""
    chats = [
        {"id": "recent", "lastMessagePreview": {"createdDateTime": "2026-09-12T12:00:00Z"}},
        {"id": "stale", "lastMessagePreview": {"createdDateTime": "2020-01-01T00:00:00Z"}},
        {"id": "staler", "lastMessagePreview": {"createdDateTime": "2019-01-01T00:00:00Z"}},
    ]

    def handler(url, params):
        if url.endswith("/me/chats"):
            return FakeResponse({"value": chats})
        return FakeResponse({"value": []})

    calls = route(monkeypatch, graph, handler)
    graph.collect_chat("tok", SINCE, UNTIL)

    queried = [c["url"] for c in calls if "/messages" in c["url"]]
    assert len(queried) == 1 and "recent" in queried[0]


def test_chat_enumeration_continues_without_preview(monkeypatch):
    """A chat with no lastMessagePreview must not end enumeration early."""
    def handler(url, params):
        if url.endswith("/me/chats"):
            return FakeResponse({"value": [{"id": "no-preview"}, {"id": "second"}]})
        return FakeResponse({"value": []})

    calls = route(monkeypatch, graph, handler)
    graph.collect_chat("tok", SINCE, UNTIL)
    assert len([c for c in calls if "/messages" in c["url"]]) == 2


def test_graph_429_raises_graph_error(monkeypatch):
    route(monkeypatch, graph, lambda url, params: FakeResponse({}, status_code=429, headers={"Retry-After": "30"}))
    with pytest.raises(graph.GraphError, match="429"):
        list(graph.get_pages("tok", "https://graph/x"))


def test_graph_http_error_raises(monkeypatch):
    route(monkeypatch, graph, lambda url, params: FakeResponse({}, status_code=403, text="Forbidden"))
    with pytest.raises(graph.GraphError, match="403"):
        list(graph.get_pages("tok", "https://graph/x"))


def test_calendar_uses_calendarview_window_params(monkeypatch):
    calls = route(monkeypatch, graph, lambda url, params: FakeResponse({"value": []}))
    graph.collect_calendar("tok", SINCE, UNTIL)
    assert calls[0]["params"]["startDateTime"] == "2026-09-12T06:00:00Z"
    assert calls[0]["params"]["endDateTime"] == "2026-09-13T06:00:00Z"


def test_todo_walks_every_list(monkeypatch):
    def handler(url, params):
        if url.endswith("/me/todo/lists"):
            return FakeResponse({"value": [{"id": "L1", "displayName": "Work"}, {"id": "L2", "displayName": "Home"}]})
        return FakeResponse({"value": [{"id": f"t-{url[-20:]}", "lastModifiedDateTime": "2026-09-12T12:00:00Z"}]})

    route(monkeypatch, graph, handler)
    items = graph.collect_todo("tok", SINCE, UNTIL)
    assert len(items) == 2
    assert {i.payload["listName"] for i in items} == {"Work", "Home"}


def test_collect_m365_partial_when_one_resource_fails(monkeypatch):
    monkeypatch.setattr(graph, "access_token", lambda alias: "tok")
    monkeypatch.setattr(graph, "collect_mail", lambda t, s, u: [Item("mail", "m1", SINCE, {})])
    monkeypatch.setattr(graph, "collect_calendar", lambda t, s, u: [])
    monkeypatch.setattr(graph, "collect_todo", lambda t, s, u: [])

    def broken_chat(t, s, u):
        raise graph.GraphError("Teams not licensed")

    monkeypatch.setattr(graph, "collect_chat", broken_chat)
    monkeypatch.setattr(graph, "COLLECTORS", {
        "mail": graph.collect_mail, "calendar": graph.collect_calendar,
        "todo": graph.collect_todo, "chat": broken_chat,
    })

    result = graph.collect_m365("work", SINCE, UNTIL)
    assert result.status == "partial"
    assert "Teams not licensed" in result.detail
    assert len(result.items) == 1  # the working resources survive


def test_collect_m365_error_when_token_dead(monkeypatch):
    def no_token(alias):
        raise graph.GraphError("AADSTS50173")

    monkeypatch.setattr(graph, "access_token", no_token)
    result = graph.collect_m365("work", SINCE, UNTIL)
    assert result.status == "error"
    assert "AADSTS50173" in result.detail


# --- Slack --------------------------------------------------------------------------


def slack_ts(dt: datetime) -> str:
    return f"{dt.timestamp():.6f}"


def test_rate_limit_canary_fires_on_15_objects(monkeypatch):
    """Exactly 15 objects with has_more, after asking for 200, is the non-Marketplace cap."""
    fifteen = [{"ts": slack_ts(UNTIL - timedelta(minutes=i)), "text": "x"} for i in range(15)]

    def handler(url, params):
        if "users.conversations" in url:
            return FakeResponse({"ok": True, "channels": [{"id": "C1", "name": "general"}]})
        return FakeResponse({"ok": True, "messages": fifteen, "has_more": True})

    route(monkeypatch, slack, handler)
    result = slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)
    assert result.status == "error"
    assert "rate-limit classified" in result.detail


def test_no_canary_when_fewer_than_cap(monkeypatch):
    def handler(url, params):
        if "users.conversations" in url:
            return FakeResponse({"ok": True, "channels": [{"id": "C1", "name": "general"}]})
        return FakeResponse({"ok": True, "messages": [{"ts": slack_ts(UNTIL), "text": "hi"}], "has_more": False})

    route(monkeypatch, slack, handler)
    assert slack.collect_slack("work", "xoxp-1", SINCE, UNTIL).status == "ok"


def test_no_canary_when_15_but_no_more(monkeypatch):
    """15 messages that are simply all there is must not be mistaken for the cap."""
    fifteen = [{"ts": slack_ts(UNTIL), "text": "x"} for _ in range(15)]

    def handler(url, params):
        if "users.conversations" in url:
            return FakeResponse({"ok": True, "channels": [{"id": "C1"}]})
        return FakeResponse({"ok": True, "messages": fifteen, "has_more": False})

    route(monkeypatch, slack, handler)
    assert slack.collect_slack("work", "xoxp-1", SINCE, UNTIL).status == "ok"


def test_system_user_filtered(monkeypatch):
    def handler(url, params):
        if "users.conversations" in url:
            return FakeResponse({"ok": True, "channels": [{"id": "C1"}]})
        return FakeResponse({"ok": True, "messages": [
            {"ts": slack_ts(UNTIL), "user": "USLACK", "text": "system"},
            {"ts": slack_ts(UNTIL), "user": "U123", "text": "real"},
        ]})

    route(monkeypatch, slack, handler)
    result = slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)
    assert [i.payload["text"] for i in result.items] == ["real"]


def test_threads_fetched_only_when_reply_count(monkeypatch):
    parent_with = {"ts": slack_ts(UNTIL), "text": "parent", "reply_count": 2}
    parent_without = {"ts": slack_ts(UNTIL - timedelta(minutes=1)), "text": "lonely"}

    def handler(url, params):
        if "users.conversations" in url:
            return FakeResponse({"ok": True, "channels": [{"id": "C1"}]})
        if "conversations.replies" in url:
            return FakeResponse({"ok": True, "messages": [
                parent_with,  # replies includes the parent; it must not be stored twice
                {"ts": slack_ts(UNTIL - timedelta(seconds=30)), "text": "reply"},
            ]})
        return FakeResponse({"ok": True, "messages": [parent_with, parent_without]})

    calls = route(monkeypatch, slack, handler)
    result = slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)

    assert len([c for c in calls if "conversations.replies" in c["url"]]) == 1
    texts = [i.payload["text"] for i in result.items]
    assert "reply" in texts
    assert texts.count("parent") == 1  # not duplicated by the replies call


def test_slack_api_error_is_error_result(monkeypatch):
    route(monkeypatch, slack, lambda url, params: FakeResponse({"ok": False, "error": "invalid_auth"}))
    result = slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)
    assert result.status == "error"
    assert "invalid_auth" in result.detail


def test_slack_missing_token(monkeypatch):
    assert slack.collect_slack("work", None, SINCE, UNTIL).status == "error"


def test_slack_oldest_is_utc_not_local(monkeypatch):
    """
    since is naive UTC; .timestamp() would read it as local time.

    Under any non-UTC container TZ that shifts the window start by the UTC offset and silently
    drops the first hours of every digest. The expected value is pinned to an explicit UTC epoch so
    this test cannot drift with the machine running it.
    """
    def handler(url, params):
        if "users.conversations" in url:
            return FakeResponse({"ok": True, "channels": [{"id": "C1"}]})
        return FakeResponse({"ok": True, "messages": []})

    calls = route(monkeypatch, slack, handler)
    slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)

    history = [c for c in calls if "conversations.history" in c["url"]][0]
    expected = SINCE.replace(tzinfo=timezone.utc).timestamp()
    assert float(history["params"]["oldest"]) == pytest.approx(expected)


def test_slack_skips_dm_types_when_configured(monkeypatch):
    """SLACK_SKIP_DMS must reach the types list, or users.conversations fails with missing_scope."""
    monkeypatch.setenv("SLACK_SKIP_DMS", "1")

    calls = route(monkeypatch, slack, lambda url, params: FakeResponse({"ok": True, "channels": []}))
    slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)

    types = calls[0]["params"]["types"]
    assert "im" not in types.split(",")
    assert "mpim" not in types.split(",")
    assert "public_channel" in types


def test_slack_includes_dm_types_by_default(monkeypatch):
    monkeypatch.delenv("SLACK_SKIP_DMS", raising=False)
    calls = route(monkeypatch, slack, lambda url, params: FakeResponse({"ok": True, "channels": []}))
    slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)
    assert "im" in calls[0]["params"]["types"].split(",")


def test_slack_skip_dms_false_means_false(monkeypatch):
    """Bare truthiness would read "false" as "skip", silently dropping every DM."""
    monkeypatch.setenv("SLACK_SKIP_DMS", "false")
    calls = route(monkeypatch, slack, lambda url, params: FakeResponse({"ok": True, "channels": []}))
    slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)
    assert "im" in calls[0]["params"]["types"].split(",")


def test_both_system_users_filtered(monkeypatch):
    """USLACK is the post-2026-06-17 system user; USLACKBOT is legacy Slackbot. Both are noise."""
    def handler(url, params):
        if "users.conversations" in url:
            return FakeResponse({"ok": True, "channels": [{"id": "C1"}]})
        return FakeResponse({"ok": True, "messages": [
            {"ts": slack_ts(UNTIL), "user": "USLACK", "text": "system"},
            {"ts": slack_ts(UNTIL), "user": "USLACKBOT", "text": "slackbot"},
            {"ts": slack_ts(UNTIL), "user": "U123", "text": "real"},
        ]})

    route(monkeypatch, slack, handler)
    result = slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)
    assert [i.payload["text"] for i in result.items] == ["real"]


# --- Zoom ---------------------------------------------------------------------------


def test_uuid_double_encoded_when_it_contains_double_slash():
    assert zoom.encode_uuid("abc//def==") == "abc%252F%252Fdef%253D%253D"


def test_uuid_single_slash_is_only_single_encoded():
    """Zoom's rule is "begins with / OR contains //" — one slash mid-string doesn't qualify."""
    assert zoom.encode_uuid("abc/def==") == "abc%2Fdef%3D%3D"


def test_uuid_double_encoded_when_it_starts_with_slash():
    assert zoom.encode_uuid("/abc==").startswith("%252F")


def test_uuid_single_encoded_otherwise():
    assert zoom.encode_uuid("abc==") == "abc%3D%3D"


def test_zoom_records_meeting_without_summary(monkeypatch):
    """A meeting whose summary never generated must be recorded, not silently dropped."""
    monkeypatch.setattr(zoom, "zoom_token", lambda: "tok")

    def handler(url, params):
        if "meeting_summaries" in url:
            return FakeResponse({"summaries": [
                {"meeting_uuid": "uuid-1", "summary_start_time": "2026-09-12T15:00:00Z"},
            ]})
        return FakeResponse({}, status_code=404)

    route(monkeypatch, zoom, handler)
    result = zoom.collect_zoom(SINCE, UNTIL)

    assert result.status == "ok"
    assert len(result.items) == 1
    assert result.items[0].payload["summary_available"] is False
    assert "without a summary" in result.detail


def test_zoom_reads_summary_content(monkeypatch):
    monkeypatch.setattr(zoom, "zoom_token", lambda: "tok")

    def handler(url, params):
        if "meeting_summaries" in url:
            return FakeResponse({"summaries": [
                {"meeting_uuid": "uuid-1", "summary_start_time": "2026-09-12T15:00:00Z"},
            ]})
        return FakeResponse({"summary_content": "# Notes\n- did a thing"})

    route(monkeypatch, zoom, handler)
    result = zoom.collect_zoom(SINCE, UNTIL)
    assert result.items[0].payload["summary_content"].startswith("# Notes")
    assert result.items[0].payload["summary_available"] is True


def test_zoom_lookback_is_wider_than_the_window(monkeypatch):
    """Summaries arrive after the meeting; a 24h query would miss last night's."""
    monkeypatch.setattr(zoom, "zoom_token", lambda: "tok")
    calls = route(monkeypatch, zoom, lambda url, params: FakeResponse({"summaries": []}))

    zoom.collect_zoom(SINCE, UNTIL)
    requested_from = calls[0]["params"]["from"]
    assert requested_from == (UNTIL - zoom.SUMMARY_LOOKBACK).strftime("%Y-%m-%d")


def test_zoom_token_failure_is_error_result(monkeypatch):
    def boom():
        raise RuntimeError("Missing Zoom credentials in .env")

    monkeypatch.setattr(zoom, "zoom_token", boom)
    result = zoom.collect_zoom(SINCE, UNTIL)
    assert result.status == "error"
    assert "Missing Zoom credentials" in result.detail


# --- dry-run CLI --------------------------------------------------------------------


def test_cli_preview_prefers_a_human_field():
    from pipeline.collect import preview

    assert preview({"subject": "Budget review"}) == "Budget review"
    assert preview({"text": "hello there"}) == "hello there"


def test_cli_preview_falls_back_to_graph_body():
    from pipeline.collect import preview

    assert preview({"body": {"content": "message body"}}) == "message body"


def test_cli_preview_handles_nothing_useful():
    from pipeline.collect import preview

    assert preview({"id": "x"}) == "(no preview field)"


def test_cli_preview_collapses_newlines_and_truncates():
    from pipeline.collect import preview

    out = preview({"text": "a\nb" + "x" * 200})
    assert "\n" not in out and len(out) <= 100


def test_cli_report_writes_nothing(monkeypatch, capsys):
    """The whole point of the dry run: no DB access at all."""
    from pipeline import collect
    from pipeline.collectors import CollectionResult

    def exploding_session(*a, **k):
        raise AssertionError("dry run must not touch the database")

    monkeypatch.setattr("pipeline.db.get_session", exploding_session)
    monkeypatch.setattr(
        collect, "dispatch",
        lambda source, since, until: CollectionResult(source, "ok", "2 things", [
            Item("mail", "m1", SINCE, {"subject": "First"}),
            Item("mail", "m2", SINCE, {"subject": "Second"}),
        ]),
    )

    assert collect.report("m365_work", hours=24, limit=5, raw=False) is True
    out = capsys.readouterr().out
    assert "status: OK" in out
    assert "First" in out and "mail=2" in out


def test_cli_report_survives_a_collector_crash(monkeypatch, capsys):
    from pipeline import collect

    def boom(source, since, until):
        raise RuntimeError("unexpected shape")

    monkeypatch.setattr(collect, "dispatch", boom)
    assert collect.report("zoom", hours=24, limit=5, raw=False) is False
    assert "CRASHED" in capsys.readouterr().out


def test_cli_report_returns_false_on_error_status(monkeypatch):
    from pipeline import collect
    from pipeline.collectors import CollectionResult

    monkeypatch.setattr(collect, "dispatch", lambda s, a, b: CollectionResult(s, "error", "dead token"))
    assert collect.report("zoom", hours=24, limit=5, raw=False) is False


# --- gmail transport ----------------------------------------------------------------


def gmail_error(status, reason=None, key="errors"):
    body = {"error": {"code": status, "message": "x"}}
    if reason:
        body["error"][key] = [{"reason": reason}]
    return FakeResponse(body, status, text=f"HTTP {status} {reason}")


@pytest.fixture
def gmail_sleeps(monkeypatch):
    """Records backoff sleeps instead of waiting, with zero jitter so delays are exact."""
    sleeps = []
    monkeypatch.setattr(gmail, "_sleep", sleeps.append)
    monkeypatch.setattr(gmail, "_random", lambda: 0.0)
    return sleeps


def test_gmail_epoch_is_utc_not_local(monkeypatch):
    """
    SINCE is naive UTC; .timestamp() would read it as local time.

    Pinned under a non-UTC TZ (UTC-7/-8 here) so the test fails on the naive form even when the
    machine running it happens to be in UTC.
    """
    expected = int(SINCE.replace(tzinfo=timezone.utc).timestamp())
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        assert int(SINCE.timestamp()) != expected  # the trap is live under this TZ
        assert gmail.to_epoch_seconds(SINCE) == expected
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_gmail_list_uses_epoch_seconds_not_date_strings(monkeypatch):
    calls = route(monkeypatch, gmail, lambda url, params: FakeResponse({"messages": []}))
    gmail.list_message_ids("tok", SINCE, UNTIL)

    after = int(SINCE.replace(tzinfo=timezone.utc).timestamp())
    before = int(UNTIL.replace(tzinfo=timezone.utc).timestamp())
    assert calls[0]["url"] == "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    assert calls[0]["params"]["q"] == f"after:{after} before:{before}"
    assert calls[0]["params"]["maxResults"] == 500


def test_gmail_list_drains_every_page(monkeypatch):
    pages = {
        None: {"messages": [{"id": "a", "threadId": "t1"}], "nextPageToken": "p2"},
        "p2": {"messages": [{"id": "b", "threadId": "t2"}], "nextPageToken": "p3"},
        "p3": {"messages": [{"id": "c", "threadId": "t3"}]},
    }
    calls = route(monkeypatch, gmail, lambda url, params: FakeResponse(pages[params.get("pageToken")]))

    found = gmail.list_message_ids("tok", SINCE, UNTIL)

    assert [m["id"] for m in found] == ["a", "b", "c"]
    # The window is re-sent with every page: pageToken alone does not carry the query.
    assert all(c["params"]["q"] == calls[0]["params"]["q"] for c in calls)
    assert "pageToken" not in calls[0]["params"]


def test_gmail_list_empty_window_has_no_messages_key(monkeypatch):
    route(monkeypatch, gmail, lambda url, params: FakeResponse({"resultSizeEstimate": 0}))
    assert gmail.list_message_ids("tok", SINCE, UNTIL) == []


def test_gmail_list_raises_rather_than_truncating_silently(monkeypatch):
    route(
        monkeypatch,
        gmail,
        lambda url, params: FakeResponse({"messages": [{"id": "x", "threadId": "t"}], "nextPageToken": "more"}),
    )
    with pytest.raises(gmail.GmailError, match="truncated"):
        gmail.list_message_ids("tok", SINCE, UNTIL)


def test_gmail_list_does_not_raise_on_the_last_allowed_page(monkeypatch):
    state = {"n": 0}

    def handler(url, params):
        state["n"] += 1
        last = state["n"] == gmail.MAX_PAGES
        return FakeResponse({"messages": [], **({} if last else {"nextPageToken": "more"})})

    route(monkeypatch, gmail, handler)
    assert gmail.list_message_ids("tok", SINCE, UNTIL) == []


@pytest.mark.parametrize(
    "first",
    [
        gmail_error(403, "rateLimitExceeded"),
        gmail_error(403, "userRateLimitExceeded"),
        gmail_error(403, "RATE_LIMIT_EXCEEDED", key="details"),
        gmail_error(429),
        gmail_error(500),
        gmail_error(502),
        gmail_error(503),
        gmail_error(504),
    ],
)
def test_gmail_call_retries_transient_failures(monkeypatch, gmail_sleeps, first):
    responses = [first, FakeResponse({"ok": True})]
    calls = route(monkeypatch, gmail, lambda url, params: responses.pop(0))

    assert gmail._call("tok", "/messages", {}) == {"ok": True}
    assert len(calls) == 2
    assert gmail_sleeps == [1.0]


def test_gmail_call_403_reads_the_status_field_too(monkeypatch, gmail_sleeps):
    body = {"error": {"code": 403, "status": "userRateLimitExceeded"}}
    responses = [FakeResponse(body, 403), FakeResponse({"ok": True})]
    route(monkeypatch, gmail, lambda url, params: responses.pop(0))
    assert gmail._call("tok", "/messages", {}) == {"ok": True}


@pytest.mark.parametrize(
    "resp",
    [
        gmail_error(403, "insufficientPermissions"),
        gmail_error(403),
        FakeResponse(None, 403, text="<html>forbidden</html>"),
        gmail_error(401, "authError"),
        gmail_error(400, "invalidArgument"),
        gmail_error(404, "notFound"),
    ],
)
def test_gmail_call_does_not_retry_permanent_errors(monkeypatch, gmail_sleeps, resp):
    calls = route(monkeypatch, gmail, lambda url, params: resp)

    with pytest.raises(gmail.GmailError, match=f"HTTP {resp.status_code}"):
        gmail._call("tok", "/messages", {})
    assert len(calls) == 1
    assert gmail_sleeps == []


def test_gmail_backoff_doubles_and_is_truncated_at_32s(monkeypatch, gmail_sleeps):
    calls = route(monkeypatch, gmail, lambda url, params: gmail_error(429))

    with pytest.raises(gmail.GmailError, match="attempts"):
        gmail._call("tok", "/messages", {})

    assert len(calls) == gmail.MAX_ATTEMPTS
    # No sleep after the final attempt: it would delay the failure and buy nothing.
    assert gmail_sleeps == [min(2.0**n, 32.0) for n in range(gmail.MAX_ATTEMPTS - 1)]
    assert max(gmail_sleeps) == 32.0


def test_gmail_backoff_never_exceeds_the_cap_with_jitter(monkeypatch):
    monkeypatch.setattr(gmail, "_random", lambda: 0.999)
    assert gmail._backoff(0) == pytest.approx(1.999)
    assert gmail._backoff(5) == 32
    assert gmail._backoff(20) == 32


def test_gmail_call_recovers_after_repeated_throttling(monkeypatch, gmail_sleeps):
    responses = [gmail_error(429), gmail_error(403, "rateLimitExceeded"), FakeResponse({"ok": True})]
    route(monkeypatch, gmail, lambda url, params: responses.pop(0))

    assert gmail._call("tok", "/messages", {}) == {"ok": True}
    assert gmail_sleeps == [1.0, 2.0]


def test_gmail_call_network_error_raises_gmail_error(monkeypatch, gmail_sleeps):
    def boom(url, headers=None, params=None, timeout=None):
        raise gmail.requests.ConnectionError("down")

    monkeypatch.setattr(gmail.requests, "get", boom)
    with pytest.raises(gmail.GmailError, match="down"):
        gmail._call("tok", "/messages", {})


def test_gmail_call_sends_bearer_token(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.update(headers=headers, timeout=timeout)
        return FakeResponse({})

    monkeypatch.setattr(gmail.requests, "get", fake_get)
    gmail._call("tok-1", "/messages", {})
    assert seen == {"headers": {"Authorization": "Bearer tok-1"}, "timeout": gmail.TIMEOUT}


# --- gmail fetch + flatten ----------------------------------------------------------

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gmail_message_multipart.json"


def gmail_fixture():
    """A fresh copy per call: tests reshape it. The fixture is synthetic (reserved example domains)."""
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def b64url(text, charset="utf-8"):
    """Unpadded base64url, the way Gmail serves body data."""
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def gmail_msg(parts=None, headers=None, **extra):
    """A minimal messages.get response; `parts` are leaf parts under a multipart/mixed root."""
    msg = {"id": "m1", "threadId": "t1", "payload": {"mimeType": "multipart/mixed", "headers": headers or [], "parts": parts or []}}
    msg.update(extra)
    return msg


def text_part(mime, text, charset=None):
    part = {"mimeType": mime, "filename": "", "body": {"data": b64url(text, charset or "utf-8")}}
    if charset:
        part["headers"] = [{"name": "Content-Type", "value": f'{mime}; charset="{charset}"'}]
    return part


def html_body(html):
    return gmail.flatten_message(gmail_msg([text_part("text/html", html)]))["bodyText"]


def test_gmail_flatten_fixture_extracts_every_field():
    out = gmail.flatten_message(gmail_fixture())

    assert out == {
        "subject": "Planning session: agenda and café booking",
        "from": "Alex Example <alex@example.com>",
        "to": "Sam Sample <sam@example.org>, team@example.org",
        "cc": "Robin Placeholder <robin@example.com>",
        "date": "Tue, 15 Sep 2026 14:30:00 +0000",
        "messageId": "<planning-0001@example.com>",
        "inReplyTo": "<planning-0000@example.com>",
        "listUnsubscribe": "<mailto:unsubscribe@example.com>",
        "threadId": "18c0ffee00000000",
        "labelIds": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
        "snippet": "Hi team, The café booking for the planning session moved to Thursday at 10:00.",
        "internalDate": "1789482605000",
        "bodyText": (
            "Hi team,\n\nThe café booking for the planning session moved to Thursday at 10:00.\n"
            "Budget check: Q2 >>> Q1 ???\n\nAgenda attached.\n\nRegards,\nAlex Example"
        ),
        "bodyTruncated": False,
        "attachments": [{"filename": "agenda.pdf", "mimeType": "application/pdf", "size": 48213}],
    }


def test_gmail_flatten_fixture_exercises_the_decode_traps():
    """Guards the fixture itself: if it stops needing padding repair or charset handling, the tests above prove less."""
    plain = gmail_fixture()["payload"]["parts"][0]["parts"][0]
    data = plain["body"]["data"]
    assert len(data) % 4 != 0  # unpadded: urlsafe_b64decode alone would raise
    assert "-" in data or "_" in data  # url-safe alphabet, not standard base64
    assert "iso-8859-1" in plain["headers"][0]["value"]  # bytes are latin-1: utf-8 would mangle the é


def test_gmail_flatten_subject_is_top_level_for_the_preview():
    from pipeline.collect import preview

    assert preview(gmail.flatten_message(gmail_fixture())).startswith("Planning session")


def test_gmail_flatten_prefers_plain_over_html():
    out = gmail.flatten_message(gmail_msg([text_part("text/html", "<p>from html</p>"), text_part("text/plain", "from plain")]))
    assert out["bodyText"] == "from plain"


def test_gmail_flatten_falls_back_to_html_when_plain_is_blank():
    out = gmail.flatten_message(gmail_msg([text_part("text/plain", " \n "), text_part("text/html", "<p>from html</p>")]))
    assert out["bodyText"] == "from html"


def test_gmail_flatten_html_only_message():
    assert html_body("<html><body><p>Hello <b>there</b></p></body></html>") == "Hello there"


def test_gmail_flatten_fixture_html_part_drops_the_injection_text():
    msg = gmail_fixture()
    alternative = msg["payload"]["parts"][0]
    alternative["parts"] = [p for p in alternative["parts"] if p["mimeType"] == "text/html"]
    body = gmail.flatten_message(msg)["bodyText"]

    assert body == "Hi team,\nThe café booking moved to Thursday at 10:00.\nAgenda attached."
    assert "SYSTEM" not in body and "ignore all previous" not in body and "reveal" not in body


@pytest.mark.parametrize(
    "open_tag",
    [
        '<div style="display:none">',
        '<div style="display: none">',
        '<div style="DISPLAY : NONE !important">',
        '<div style="color:red; visibility:hidden">',
        '<div style="VISIBILITY: Hidden">',
        "<div hidden>",
        '<div hidden="hidden">',
        '<div aria-hidden="true">',
        '<div aria-hidden="TRUE">',
    ],
)
def test_gmail_html_hidden_content_is_dropped(open_tag):
    assert html_body(f"<p>shown</p>{open_tag}secret</div><p>after</p>") == "shown\nafter"


def test_gmail_html_hidden_nesting_drops_inner_text_and_keeps_text_after_the_close():
    assert html_body('<div style="display:none"><span>x</span></div>tail') == "tail"
    assert html_body("<div hidden><div>a</div>b</div>c") == "c"
    assert html_body('<div hidden><div hidden>a</div>b</div>c') == "c"


def test_gmail_html_visible_child_of_a_hidden_parent_stays_hidden():
    assert html_body('<div hidden><p style="display:block">still secret</p></div>after') == "after"


def test_gmail_html_unclosed_children_do_not_leak_or_swallow():
    """<p>/<li> are routinely left open; the hidden region must still end at its own close tag."""
    assert html_body("<div hidden><p>secret<p>more<li>item</div>visible") == "visible"


def test_gmail_html_aria_hidden_false_is_visible():
    assert html_body('<div aria-hidden="false">shown</div>') == "shown"


def test_gmail_html_void_tags_never_open_a_hidden_region():
    assert html_body('<input aria-hidden="true">one<img hidden src="x">two<br hidden>three<hr hidden>four') == "onetwo\nthreefour"
    assert html_body('<meta hidden><link hidden>five') == "five"


def test_gmail_html_script_style_head_title_are_dropped():
    html = (
        "<html><head><title>T</title><style>p{}</style></head><body>"
        "<script>var leaked = 1;</script><p>body</p><style>.x{}</style></body></html>"
    )
    assert html_body(html) == "body"


def test_gmail_html_block_tags_become_newlines_and_blank_runs_collapse():
    html = "<p>a</p><p></p><p></p><div>b</div>c<br>d<ul><li>x</li><li>y</li></ul><table><tr><td>t1</td></tr><tr><td>t2</td></tr></table>"
    assert html_body(html) == "a\nb\nc\nd\nx\ny\nt1\nt2"
    assert html_body("a<br><br><br><br>b") == "a\n\nb"  # deliberate <br> runs collapse to one blank line
    assert html_body("<div>\n  <p>a</p>\n  <p>b</p>\n</div>") == "a\nb"  # source formatting adds no blank lines


def test_gmail_html_entities_and_nbsp_are_decoded_and_spacing_collapsed():
    assert html_body("<p>Tom &amp; Jerry&nbsp;&nbsp;   ok\n\n  fine &#233;</p>") == "Tom & Jerry ok fine é"


def test_gmail_flatten_missing_headers_are_none_and_nothing_raises():
    out = gmail.flatten_message({"id": "m1"})
    assert out["subject"] is None and out["from"] is None and out["cc"] is None
    assert out["messageId"] is None and out["inReplyTo"] is None and out["listUnsubscribe"] is None
    assert out["bodyText"] == "" and out["bodyTruncated"] is False
    assert out["labelIds"] == [] and out["attachments"] == [] and out["snippet"] == ""
    assert out["internalDate"] is None and out["threadId"] is None


def test_gmail_flatten_header_names_are_case_insensitive_and_values_unfolded():
    headers = [{"name": "SUBJECT", "value": "a\r\n  folded   subject"}, {"name": "sUbJeCt", "value": "second loses"}]
    assert gmail.flatten_message(gmail_msg(headers=headers))["subject"] == "a folded subject"


def test_gmail_flatten_caps_the_body_and_flags_truncation(monkeypatch):
    monkeypatch.setattr(gmail, "BODY_CAP", 10)
    over = gmail.flatten_message(gmail_msg([text_part("text/plain", "x" * 25)]))
    assert over["bodyText"] == "x" * 10 and over["bodyTruncated"] is True

    exact = gmail.flatten_message(gmail_msg([text_part("text/plain", "x" * 10)]))
    assert exact["bodyText"] == "x" * 10 and exact["bodyTruncated"] is False


def test_gmail_body_cap_default():
    assert gmail.BODY_CAP == 20000


def test_gmail_flatten_charset_falls_back_to_utf8_when_absent_or_unknown():
    unknown = {"mimeType": "text/plain", "body": {"data": b64url("café")}, "headers": [{"name": "Content-Type", "value": "text/plain; charset=bogus-9"}]}
    absent = {"mimeType": "text/plain", "body": {"data": b64url("café")}}
    assert gmail.flatten_message(gmail_msg([unknown]))["bodyText"] == "café"
    assert gmail.flatten_message(gmail_msg([absent]))["bodyText"] == "café"


def test_gmail_flatten_undecodable_bytes_are_replaced_not_raised():
    bad = base64.urlsafe_b64encode(b"ok \xff\xfe end").decode().rstrip("=")
    part = {"mimeType": "text/plain", "body": {"data": bad}}
    assert gmail.flatten_message(gmail_msg([part]))["bodyText"] == "ok \ufffd\ufffd end"


def test_gmail_flatten_corrupt_part_is_skipped_not_fatal():
    corrupt = {"mimeType": "text/plain", "body": {"data": "a"}}  # one base64 char cannot be padded into valid data
    out = gmail.flatten_message(gmail_msg([corrupt, text_part("text/html", "<p>still here</p>")]))
    assert out["bodyText"] == "still here"


def test_gmail_flatten_named_text_part_is_an_attachment_not_body():
    notes = {"mimeType": "text/plain", "filename": "notes.txt", "body": {"attachmentId": "A1", "size": 12}}
    out = gmail.flatten_message(gmail_msg([notes, text_part("text/plain", "real body")]))
    assert out["bodyText"] == "real body"
    assert out["attachments"] == [{"filename": "notes.txt", "mimeType": "text/plain", "size": 12}]


def test_gmail_flatten_part_without_data_contributes_nothing():
    big = {"mimeType": "text/plain", "filename": "", "body": {"attachmentId": "A2", "size": 900000}}
    assert gmail.flatten_message(gmail_msg([big]))["bodyText"] == ""


def test_gmail_flatten_walks_deeply_nested_parts_in_order():
    inner = {"mimeType": "multipart/alternative", "parts": [text_part("text/plain", "deep")]}
    outer = {"mimeType": "multipart/related", "parts": [inner]}
    assert gmail.flatten_message(gmail_msg([outer]))["bodyText"] == "deep"


def test_gmail_parse_internal_date_is_naive_utc_under_a_non_utc_tz():
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        got = gmail.parse_internal_date("1789482605000")
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    assert got == datetime(2026, 9, 15, 14, 30, 5)
    assert got.tzinfo is None


def test_gmail_parse_internal_date_keeps_milliseconds():
    assert gmail.parse_internal_date("1789482605123") == datetime(2026, 9, 15, 14, 30, 5, 123000)


@pytest.mark.parametrize("value", [None, "", "abc", "12.5", [], {}, "9" * 30])
def test_gmail_parse_internal_date_unparseable_is_none(value):
    assert gmail.parse_internal_date(value) is None


def test_gmail_flatten_keeps_the_original_internal_date_string():
    assert gmail.flatten_message(gmail_msg(internalDate="not-a-number"))["internalDate"] == "not-a-number"


def test_gmail_fetch_message_is_a_single_full_format_get(monkeypatch):
    """Exactly one request: the attachment part carries an attachmentId, and attachments.get must never fire."""
    calls = route(monkeypatch, gmail, lambda url, params: FakeResponse(gmail_fixture()))

    msg = gmail.fetch_message("tok", "18c0ffee00000001")
    gmail.flatten_message(msg)

    assert len(calls) == 1
    assert calls[0]["url"] == "https://gmail.googleapis.com/gmail/v1/users/me/messages/18c0ffee00000001"
    assert calls[0]["params"] == {"format": "full"}
    assert "attachments" not in calls[0]["url"]


def test_gmail_fetch_message_quotes_the_id(monkeypatch):
    calls = route(monkeypatch, gmail, lambda url, params: FakeResponse({}))
    gmail.fetch_message("tok", "a/../b?x=1")
    assert calls[0]["url"] == "https://gmail.googleapis.com/gmail/v1/users/me/messages/a%2F..%2Fb%3Fx%3D1"


def test_gmail_fetch_message_error_raises_gmail_error(monkeypatch):
    route(monkeypatch, gmail, lambda url, params: gmail_error(404, "notFound"))
    with pytest.raises(gmail.GmailError, match="HTTP 404"):
        gmail.fetch_message("tok", "gone")


def test_gmail_fetch_messages_paces_between_gets_and_collects_failures(monkeypatch, gmail_sleeps):
    def handler(url, params):
        if url.endswith("/bad"):
            return gmail_error(404, "notFound")
        return FakeResponse({"id": url.rsplit("/", 1)[1]})

    calls = route(monkeypatch, gmail, handler)
    messages, failures = gmail.fetch_messages("tok", ["a", "bad", "c"])

    assert [m["id"] for m in messages] == ["a", "c"]
    assert [f["id"] for f in failures] == ["bad"] and "404" in failures[0]["error"]
    assert len(calls) == 3
    assert gmail_sleeps == [gmail.GET_PACE_SECONDS, gmail.GET_PACE_SECONDS]  # between gets, never before the first


def test_gmail_fetch_messages_empty_and_single_do_not_sleep(monkeypatch, gmail_sleeps):
    route(monkeypatch, gmail, lambda url, params: FakeResponse({"id": "a"}))
    assert gmail.fetch_messages("tok", []) == ([], [])
    assert gmail.fetch_messages("tok", ["a"]) == ([{"id": "a"}], [])
    assert gmail_sleeps == []


def test_gmail_get_pace_keeps_the_per_user_quota():
    """20 units per get against 6,000/min: the pace alone must stay under the ceiling of 300 gets/min."""
    assert 60 / gmail.GET_PACE_SECONDS * 20 < 6000


# --- gmail collect_gmail orchestration ----------------------------------------------


def epoch_ms(moment):
    """internalDate as Gmail serves it: milliseconds since epoch, as a string."""
    return str(gmail.to_epoch_seconds(moment) * 1000)


INSIDE = SINCE + timedelta(hours=2)


@pytest.fixture
def gmail_token(monkeypatch):
    """pipeline.health.gmail_token_response is a network POST to Google: the true external boundary."""
    seen = []

    def fake(label):
        seen.append(label)
        return {"access_token": "tok-" + label}

    monkeypatch.setattr("pipeline.health.gmail_token_response", fake)
    return seen


def gmail_mailbox(monkeypatch, messages, listed=None, fail=None):
    """
    Route list + get over `messages` ({id: messages.get body}). `listed` overrides the id list
    (to repeat ids); `fail` maps an id to a FakeResponse returned for its get.
    """
    fail = fail or {}
    ids = listed if listed is not None else list(messages)

    def handler(url, params):
        if url.endswith("/messages"):
            return FakeResponse({"messages": [{"id": i, "threadId": "t"} for i in ids]})
        message_id = url.rsplit("/", 1)[1]
        return fail.get(message_id) or FakeResponse(messages[message_id])

    return route(monkeypatch, gmail, handler)


def mail(message_id, when=INSIDE, subject="Hello", **extra):
    extra.setdefault("internalDate", epoch_ms(when))
    msg = gmail_msg([text_part("text/plain", "body")], [{"name": "Subject", "value": subject}], **extra)
    msg["id"] = message_id
    return msg


def test_gmail_collect_ok_builds_mail_items(monkeypatch, gmail_token, gmail_sleeps):
    gmail_mailbox(monkeypatch, {"a": mail("a", INSIDE, "First"), "b": mail("b", INSIDE + timedelta(minutes=5), "Second")})

    result = gmail.collect_gmail("x", "refresh", SINCE, UNTIL)

    assert result.status == "ok" and result.detail == "2 messages"
    assert gmail_token == ["x"] and result.source == "gmail_x"
    assert [(i.item_type, i.external_id, i.occurred_at) for i in result.items] == [
        ("mail", "a", INSIDE),
        ("mail", "b", INSIDE + timedelta(minutes=5)),
    ]
    assert result.items[0].payload["subject"] == "First"
    assert result.items[0].payload["bodyText"] == "body"


def test_gmail_collect_missing_refresh_token_is_error(monkeypatch, gmail_token):
    calls = gmail_mailbox(monkeypatch, {})
    for missing in (None, ""):
        result = gmail.collect_gmail("x", missing, SINCE, UNTIL)
        assert result.status == "error" and "token" in result.detail
    assert gmail_token == [] and calls == []


@pytest.mark.parametrize(
    "exc, expected",
    [
        (lambda: health.GmailAuthError("invalid_grant — re-run auth/gmail_bootstrap.py x"), "re-run auth/gmail_bootstrap.py"),
        (lambda: RuntimeError("Missing GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env"), "GMAIL_CLIENT_ID"),
        (lambda: requests.ConnectionError("dns down"), "dns down"),
    ],
)
def test_gmail_collect_token_failures_are_errors_not_raises(monkeypatch, exc, expected):
    def fail(label):
        raise exc()

    monkeypatch.setattr("pipeline.health.gmail_token_response", fail)
    calls = gmail_mailbox(monkeypatch, {})

    result = gmail.collect_gmail("x", "refresh", SINCE, UNTIL)

    assert result.status == "error" and expected in result.detail
    assert calls == []


def test_gmail_collect_list_failure_is_error(monkeypatch, gmail_token, gmail_sleeps):
    route(monkeypatch, gmail, lambda url, params: gmail_error(403, "insufficientPermissions"))
    result = gmail.collect_gmail("x", "refresh", SINCE, UNTIL)
    assert result.status == "error" and "HTTP 403" in result.detail and result.items == []


def test_gmail_collect_empty_window_is_ok(monkeypatch, gmail_token):
    route(monkeypatch, gmail, lambda url, params: FakeResponse({"resultSizeEstimate": 0}))
    result = gmail.collect_gmail("x", "refresh", SINCE, UNTIL)
    assert (result.status, result.detail, result.items) == ("ok", "0 messages", [])


def test_gmail_collect_one_get_failing_past_retries_is_partial_and_keeps_items(monkeypatch, gmail_token, gmail_sleeps):
    calls = gmail_mailbox(
        monkeypatch,
        {"a": mail("a"), "c": mail("c")},
        listed=["a", "bad", "c"],
        fail={"bad": gmail_error(500)},
    )

    result = gmail.collect_gmail("x", "refresh", SINCE, UNTIL)

    assert result.status == "partial" and result.ok
    assert [i.external_id for i in result.items] == ["a", "c"]
    assert result.detail.startswith("2 messages (1 failed: bad: ") and "still failing" in result.detail
    assert sum(c["url"].endswith("/bad") for c in calls) == gmail.MAX_ATTEMPTS
    assert "\n" not in result.detail


def test_gmail_collect_every_get_failing_is_error(monkeypatch, gmail_token, gmail_sleeps):
    gmail_mailbox(monkeypatch, {}, listed=["a", "b"], fail={"a": gmail_error(404, "notFound"), "b": gmail_error(404, "notFound")})
    result = gmail.collect_gmail("x", "refresh", SINCE, UNTIL)
    assert result.status == "error" and not result.ok and result.items == []
    assert "2 failed" in result.detail


def test_gmail_collect_window_is_since_inclusive_until_exclusive(monkeypatch, gmail_token, gmail_sleeps):
    gmail_mailbox(
        monkeypatch,
        {
            "before": mail("before", SINCE - timedelta(seconds=1)),
            "at_since": mail("at_since", SINCE),
            "at_until": mail("at_until", UNTIL),
            "after": mail("after", UNTIL + timedelta(hours=1)),
        },
    )
    result = gmail.collect_gmail("x", "refresh", SINCE, UNTIL)
    assert result.status == "ok" and result.detail == "1 messages"
    assert [i.external_id for i in result.items] == ["at_since"]


def test_gmail_collect_unparseable_internal_date_is_skipped_and_counted(monkeypatch, gmail_token, gmail_sleeps):
    gmail_mailbox(monkeypatch, {"a": mail("a"), "junk": mail("junk", internalDate="not-a-number"), "gone": gmail_msg(id="gone")})

    result = gmail.collect_gmail("x", "refresh", SINCE, UNTIL)

    assert result.status == "ok"  # skipped is not failed
    assert [i.external_id for i in result.items] == ["a"]
    assert result.detail == "1 messages, 2 skipped (no usable internalDate)"


def test_gmail_collect_repeated_ids_are_fetched_once(monkeypatch, gmail_token, gmail_sleeps):
    calls = gmail_mailbox(monkeypatch, {"a": mail("a"), "b": mail("b")}, listed=["a", "b", "a", "b", "a"])

    result = gmail.collect_gmail("x", "refresh", SINCE, UNTIL)

    assert [i.external_id for i in result.items] == ["a", "b"]
    assert sum(not c["url"].endswith("/messages") for c in calls) == 2


def test_gmail_collect_sends_the_access_token_not_the_refresh_token(monkeypatch, gmail_token, gmail_sleeps):
    seen = []

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.append(headers["Authorization"])
        return FakeResponse({})

    monkeypatch.setattr(gmail.requests, "get", fake_get)
    gmail.collect_gmail("x", "the-refresh-token", SINCE, UNTIL)
    assert seen == ["Bearer tok-x"]


def test_gmail_dispatch_routes_to_collect_gmail_with_label_and_env_token(monkeypatch, gmail_token, gmail_sleeps):
    from pipeline.collectors import dispatch

    monkeypatch.setenv("GMAIL_X_REFRESH_TOKEN", "refresh-from-env")
    gmail_mailbox(monkeypatch, {"a": mail("a", INSIDE, "Routed")})

    result = dispatch("gmail_x", SINCE, UNTIL)

    assert result.source == "gmail_x" and result.status == "ok"
    assert gmail_token == ["x"]
    assert result.items[0].payload["subject"] == "Routed"


def test_gmail_dispatch_without_a_configured_token_is_error(monkeypatch, gmail_token):
    from pipeline.collectors import dispatch

    monkeypatch.delenv("GMAIL_NOLABEL_REFRESH_TOKEN", raising=False)
    result = dispatch("gmail_nolabel", SINCE, UNTIL)
    assert result.status == "error" and "token" in result.detail.lower()
    assert gmail_token == []


def test_gmail_report_previews_the_subject_line(monkeypatch, gmail_token, gmail_sleeps, capsys):
    from pipeline import collect
    from pipeline.collectors import dispatch

    monkeypatch.setenv("GMAIL_X_REFRESH_TOKEN", "refresh-from-env")
    gmail_mailbox(monkeypatch, {"a": mail("a", INSIDE, "Quarterly numbers are in")})
    # report() windows on the real clock; pin it to the fixture window so the item lands inside.
    monkeypatch.setattr(collect, "dispatch", lambda s, a, b: dispatch(s, SINCE, UNTIL))

    assert collect.report("gmail_x", hours=24, limit=5, raw=False) is True
    out = capsys.readouterr().out
    assert "Quarterly numbers are in" in out and "mail=1" in out
