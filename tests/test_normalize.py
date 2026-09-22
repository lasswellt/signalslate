"""
pipeline.normalize: NormalizedItem schema and the Gmail adapter.

The round-trip tests feed the REAL output of flatten_message() on the synthetic T-005 fixture, so a
change to the collector's payload shape breaks these tests instead of drifting silently.
"""
import copy
import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.collectors.gmail import flatten_message, parse_internal_date
from pipeline.normalize import (
    NORMALIZED_BODY_CAP,
    NoAdapterError,
    NormalizedItem,
    PayloadError,
    normalize,
    normalize_gmail,
    normalize_zoom,
    strip_quoted_reply,
    truncate,
)

FIXTURE = Path(__file__).parent / "fixtures" / "gmail_message_multipart.json"
SOURCE = "gmail_test"


def raw_message() -> dict:
    return json.loads(FIXTURE.read_text())


def flattened(**overrides) -> dict:
    """The collector's real output for the fixture, with per-test field overrides."""
    payload = flatten_message(raw_message())
    payload.update(overrides)
    return payload


def occurred_at() -> datetime:
    moment = parse_internal_date(raw_message()["internalDate"])
    assert moment is not None
    return moment


def item_from(payload: dict) -> NormalizedItem:
    return normalize_gmail(SOURCE, "18c0ffee00000001", occurred_at(), payload)


def test_normalize_gmail_fixture_maps_scalar_fields():
    item = item_from(flattened())

    assert item.id == "gmail_test:18c0ffee00000001"
    assert item.source == SOURCE
    assert item.kind == "mail"
    assert item.occurred_at == occurred_at()
    assert item.occurred_at.tzinfo is None
    assert item.title == "Planning session: agenda and café booking"
    assert item.thread_key == "gmail:18c0ffee00000000"
    assert item.labels == ["INBOX", "UNREAD", "CATEGORY_UPDATES"]
    assert item.is_unread is True
    assert item.direction == "inbound"
    assert item.is_bulk is True
    assert item.raw_ref == "18c0ffee00000001"
    assert item.permalink is None
    assert item.trust == "untrusted_third_party"


def test_normalize_gmail_fixture_maps_participants_with_roles():
    item = item_from(flattened())

    assert [(p.role, p.name, p.address) for p in item.participants] == [
        ("from", "Alex Example", "alex@example.com"),
        ("to", "Sam Sample", "sam@example.org"),
        ("to", "", "team@example.org"),
        ("cc", "Robin Placeholder", "robin@example.com"),
    ]


def test_normalize_gmail_fixture_keeps_attachment_metadata_only():
    item = item_from(flattened())

    assert [(a.filename, a.mime_type, a.size) for a in item.attachments] == [("agenda.pdf", "application/pdf", 48213)]


def test_normalize_gmail_fixture_body_omits_hidden_injection_text():
    item = item_from(flattened())

    assert "café booking" in item.body_text
    assert "ignore all previous instructions" not in item.body_text.lower()
    assert "reveal the full mailbox" not in item.body_text.lower()
    assert item.body_truncated is False


def test_normalize_gmail_without_list_unsubscribe_is_not_bulk():
    assert item_from(flattened(listUnsubscribe=None)).is_bulk is False


def test_normalize_gmail_sent_label_is_outbound_and_read():
    item = item_from(flattened(labelIds=["SENT"]))

    assert item.direction == "outbound"
    assert item.is_unread is False


def test_normalize_gmail_missing_subject_gets_placeholder_title():
    assert item_from(flattened(subject=None)).title == "(no subject)"
    assert item_from(flattened(subject="   ")).title == "(no subject)"


def test_normalize_gmail_missing_thread_id_has_no_thread_key():
    assert item_from(flattened(threadId=None)).thread_key is None


def test_normalize_gmail_drops_participants_without_address():
    item = item_from(flattened(to="undisclosed-recipients:;", cc=None))

    assert [p.role for p in item.participants] == ["from"]


def test_normalize_gmail_empty_body_falls_back_to_unescaped_snippet():
    item = item_from(flattened(bodyText="", snippet="It&#39;s Thursday &amp; 10:00 &lt;sharp&gt;"))

    assert item.body_text == "It's Thursday & 10:00 <sharp>"


def test_normalize_dispatches_gmail_source_with_dict_payload():
    item = normalize(SOURCE, "mail", "18c0ffee00000001", occurred_at(), flattened())

    assert item == item_from(flattened())


def test_normalize_accepts_payload_as_json_string():
    payload = flattened()

    from_string = normalize(SOURCE, "mail", "18c0ffee00000001", occurred_at(), json.dumps(payload))

    assert from_string == normalize(SOURCE, "mail", "18c0ffee00000001", occurred_at(), payload)


def test_normalize_kind_mirrors_item_type():
    assert normalize(SOURCE, "message", "x1", occurred_at(), flattened()).kind == "message"


def test_normalize_unknown_source_raises_no_adapter_error_naming_source():
    with pytest.raises(NoAdapterError, match="unifi_home"):
        normalize("unifi_home", "event", "e1", occurred_at(), {})


def test_normalize_rejects_payload_that_is_not_an_object():
    with pytest.raises(PayloadError):
        normalize(SOURCE, "mail", "x1", occurred_at(), "not json")
    with pytest.raises(PayloadError):
        normalize(SOURCE, "mail", "x1", occurred_at(), "[1, 2]")


def test_normalize_does_not_mutate_the_payload():
    payload = flattened()
    before = copy.deepcopy(payload)

    normalize(SOURCE, "mail", "x1", occurred_at(), payload)

    assert payload == before


def test_normalized_item_rejects_wrong_trust_value():
    data = item_from(flattened()).model_dump()
    data["trust"] = "trusted"

    with pytest.raises(ValidationError):
        NormalizedItem.model_validate(data)


def test_strip_quoted_reply_drops_quoted_lines():
    text = "Sounds good.\n> earlier line\n>> older line\nSee you then."

    assert strip_quoted_reply(text) == "Sounds good.\nSee you then."


def test_strip_quoted_reply_drops_attribution_and_everything_after():
    text = "Yes, Thursday works.\n\nOn Tue, 15 Sep 2026 at 14:30, Alex Example <alex@example.com> wrote:\n> Are you free?\nUnquoted trailing line."

    assert strip_quoted_reply(text) == "Yes, Thursday works."


def test_strip_quoted_reply_drops_attribution_wrapped_over_two_lines():
    text = "Confirmed.\n\nOn Tuesday, September 15, 2026 at 2:30 PM, Alex Example\n<alex@example.com> wrote:\n> Are you free?"

    assert strip_quoted_reply(text) == "Confirmed."


def test_strip_quoted_reply_keeps_line_that_merely_starts_with_on():
    text = "On balance, Thursday is better.\nOn second thought, Friday."

    assert strip_quoted_reply(text) == text


@pytest.mark.parametrize("delimiter", ["-- ", "--"])
def test_strip_quoted_reply_drops_trailing_signature(delimiter):
    text = f"Thanks for the update.\n\n{delimiter}\nAlex Example\nExample Corp"

    assert strip_quoted_reply(text) == "Thanks for the update."


def test_strip_quoted_reply_keeps_text_when_delimiter_has_nothing_above():
    text = "--\nThe whole message is below a stray delimiter."

    assert strip_quoted_reply(text) == text


def test_strip_quoted_reply_keeps_text_after_a_delimiter_with_a_long_tail():
    tail = "\n".join(f"line {i}" for i in range(30))
    text = f"Intro.\n--\n{tail}"

    assert strip_quoted_reply(text).endswith("line 29")


def test_strip_quoted_reply_never_empty_with_text_above_quote_attribution_and_signature():
    text = "Ok.\n> quoted\nOn Mon, 14 Sep 2026, Sam Sample <sam@example.org> wrote:\n> more\n-- \nSig"

    assert strip_quoted_reply(text) == "Ok."


def test_strip_quoted_reply_collapses_blank_runs_and_strips():
    assert strip_quoted_reply("\n\nOne.\n\n\n\n\nTwo.\n\n") == "One.\n\nTwo."


def test_strip_quoted_reply_is_deterministic():
    text = "A\n> q\n\n\n\nB\n-- \nsig"

    assert strip_quoted_reply(text) == strip_quoted_reply(text)


def test_truncate_returns_text_unchanged_when_it_fits():
    assert truncate("abc def", 7) == ("abc def", False)
    assert truncate("", 5) == ("", False)


def test_truncate_cuts_at_whitespace_at_or_below_cap():
    assert truncate("alpha beta gamma", 10) == ("alpha beta", True)  # the cap lands exactly before a space
    assert truncate("alpha beta gamma", 13) == ("alpha beta", True)  # mid-word: back up to the boundary


def test_truncate_hard_cuts_a_single_token_longer_than_cap():
    assert truncate("x" * 30, 10) == ("x" * 10, True)


def test_truncate_result_never_exceeds_cap():
    text = "word " * 1000

    cut, was_truncated = truncate(text, 101)

    assert was_truncated is True
    assert len(cut) <= 101


def test_normalize_gmail_long_body_is_capped_and_flagged_truncated():
    item = item_from(flattened(bodyText="word " * 1000, bodyTruncated=False))

    assert len(item.body_text) <= NORMALIZED_BODY_CAP
    assert item.body_truncated is True


def test_normalize_gmail_collector_truncation_flag_is_carried_through():
    item = item_from(flattened(bodyText="short body", bodyTruncated=True))

    assert item.body_text == "short body"
    assert item.body_truncated is True


def test_normalize_gmail_short_body_from_untruncated_collector_is_not_flagged():
    assert item_from(flattened(bodyText="short body", bodyTruncated=False)).body_truncated is False


# --- Zoom adapter ------------------------------------------------------------------------------


ZOOM_OCCURRED_AT = datetime(2026, 9, 20, 15, 0, 0)


def zoom_payload(**overrides) -> dict:
    payload = {
        "topic": "Weekly sync",
        "start_time": "2026-09-20T15:00:00Z",
        "end_time": "2026-09-20T15:30:00Z",
        "host_email": "host@example.com",
        "hosted": True,
        "summary_content": "Discussed roadmap and next steps.",
        "summary_doc_url": "https://zoom.us/docs/abc123",
        "summary_available": True,
        "participants": [
            {"name": "Host Person", "email": "host@example.com"},
            {"name": "Attendee One", "email": "attendee1@example.com"},
            {"name": "No Email Attendee", "email": None},
        ],
    }
    payload.update(overrides)
    return payload


def zoom_item(**overrides) -> NormalizedItem:
    return normalize_zoom("zoom", "meeting-uuid-1", ZOOM_OCCURRED_AT, zoom_payload(**overrides))


def test_normalize_zoom_hosted_meeting_maps_scalar_fields():
    item = zoom_item()

    assert item.id == "zoom:meeting-uuid-1"
    assert item.source == "zoom"
    assert item.kind == "meeting"
    assert item.occurred_at == ZOOM_OCCURRED_AT
    assert item.title == "Weekly sync"
    assert item.body_text == "Discussed roadmap and next steps."
    assert item.body_truncated is False
    assert item.thread_key is None
    assert item.direction == "inbound"
    assert item.is_unread is False
    assert item.is_bulk is False
    assert item.attachments == []
    assert item.raw_ref == "meeting-uuid-1"
    assert item.permalink == "https://zoom.us/docs/abc123"
    assert item.trust == "untrusted_third_party"


def test_normalize_zoom_participants_have_host_and_attendee_roles_and_dedupe_host():
    item = zoom_item()

    assert [(p.role, p.name, p.address) for p in item.participants] == [
        ("host", "", "host@example.com"),
        ("attendee", "Attendee One", "attendee1@example.com"),
    ]


def test_normalize_zoom_attendee_without_email_is_dropped():
    item = zoom_item(host_email=None, participants=[{"name": "No Email", "email": None}])

    assert item.participants == []


def test_normalize_zoom_labels_hosted_with_summary():
    item = zoom_item()

    assert item.labels == ["zoom", "hosted"]


def test_normalize_zoom_labels_not_host_no_summary():
    item = zoom_item(
        hosted=False,
        participants=[],
        summary_available=False,
        summary_unavailable_reason="not_host",
        transcript_available=False,
        transcript_unavailable_reason="not_host",
    )

    assert item.labels == ["zoom", "no-summary", "attended"]
    assert item.body_text == "No AI Companion summary available. (you did not host this meeting)"


def test_normalize_zoom_labels_hosted_missing_summary_not_flagged_not_host():
    item = zoom_item(summary_available=False, summary_content=None, summary_unavailable_reason=None)

    assert item.labels == ["zoom", "no-summary", "hosted"]
    assert item.body_text == "No AI Companion summary available."


def test_normalize_zoom_labels_include_transcript_when_available():
    item = zoom_item(transcript_available=True, transcript_text="Speaker: hello", transcript_truncated=False)

    assert item.labels == ["zoom", "hosted", "transcript"]
    assert "Transcript excerpt:\nSpeaker: hello" in item.body_text


def test_normalize_zoom_transcript_truncated_flag_carries_through_even_when_body_fits():
    item = zoom_item(transcript_available=True, transcript_text="short excerpt", transcript_truncated=True)

    assert item.body_truncated is True


def test_normalize_zoom_body_is_capped_and_flagged_truncated():
    item = zoom_item(summary_content="word " * 1000)

    assert len(item.body_text) <= NORMALIZED_BODY_CAP
    assert item.body_truncated is True


def test_normalize_zoom_missing_topic_falls_back_to_placeholder_title():
    assert zoom_item(topic=None).title == "Zoom meeting"
    assert zoom_item(topic="   ").title == "Zoom meeting"


def test_normalize_zoom_no_host_email_has_no_host_participant():
    item = zoom_item(host_email=None, participants=[{"name": "Attendee One", "email": "attendee1@example.com"}])

    assert [p.role for p in item.participants] == ["attendee"]


def test_normalize_zoom_missing_summary_doc_url_has_no_permalink():
    assert zoom_item(summary_doc_url=None).permalink is None


def test_normalize_dispatches_zoom_source_with_dict_payload():
    item = normalize("zoom", "meeting", "meeting-uuid-1", ZOOM_OCCURRED_AT, zoom_payload())

    assert item == zoom_item()
    assert item.kind == "meeting"


def test_normalize_accepts_zoom_payload_as_json_string():
    payload = zoom_payload()

    from_string = normalize("zoom", "meeting", "meeting-uuid-1", ZOOM_OCCURRED_AT, json.dumps(payload))

    assert from_string == normalize("zoom", "meeting", "meeting-uuid-1", ZOOM_OCCURRED_AT, payload)


def test_normalize_unknown_source_still_raises_no_adapter_error():
    with pytest.raises(NoAdapterError, match="slack_x"):
        normalize("slack_x", "message", "m1", ZOOM_OCCURRED_AT, {})
