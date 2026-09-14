"""
Collector tests. No network: requests is stubbed at each collector module's own import site,
following tests/test_health.py's monkeypatch-the-module style.

The traps these pin, all from docs/_research/2026-09-13_phase2-collectors.md:
  - Graph ignores $filter unless $orderby names the same property (silent, returns everything).
  - Slack's non-Marketplace cap returns exactly 15 objects regardless of the requested limit.
  - Zoom UUIDs containing "/" must be double-encoded.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.collectors import Item, parse_iso, parse_slack_ts, to_graph_time  # noqa: E402
from pipeline.collectors import graph, slack, zoom  # noqa: E402

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
    assert [i.external_id for i in items] == ["m-new"]


def test_chat_drops_system_event_messages(monkeypatch):
    def handler(url, params):
        if url.endswith("/me/chats"):
            return FakeResponse({"value": [{"id": "19:abc"}]})
        return FakeResponse({"value": [
            {"id": "sys", "messageType": "systemEventMessage", "lastModifiedDateTime": "2026-09-12T12:00:00Z"},
            {"id": "real", "messageType": "message", "lastModifiedDateTime": "2026-09-12T12:00:00Z"},
        ]})

    route(monkeypatch, graph, handler)
    assert [i.external_id for i in graph.collect_chat("tok", SINCE, UNTIL)] == ["real"]


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


def test_pagination_stops_at_max_pages(monkeypatch):
    route(monkeypatch, graph, lambda url, params: FakeResponse(
        {"value": [{"id": "x"}], "@odata.nextLink": "https://graph/next"}
    ))
    assert len(list(graph.get_pages("tok", "https://graph/start"))) == graph.MAX_PAGES


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


def test_slack_oldest_is_epoch_seconds(monkeypatch):
    def handler(url, params):
        if "users.conversations" in url:
            return FakeResponse({"ok": True, "channels": [{"id": "C1"}]})
        return FakeResponse({"ok": True, "messages": []})

    calls = route(monkeypatch, slack, handler)
    slack.collect_slack("work", "xoxp-1", SINCE, UNTIL)
    history = [c for c in calls if "conversations.history" in c["url"]][0]
    assert float(history["params"]["oldest"]) == pytest.approx(SINCE.timestamp())


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
