"""
pipeline.triage: schema, aliasing, batching, output validation and the deterministic stub.

Everything under test is pure, so nothing is mocked. Items are built through the real Gmail adapter
(normalize_gmail) so a change to NormalizedItem breaks these tests instead of drifting silently.
"""
from datetime import datetime
from typing import Any, Optional

import pytest
from pydantic import ValidationError

from pipeline.normalize import NO_SUBJECT, NormalizedItem, Participant, normalize_gmail
from pipeline.triage import (
    ACTION_TEXT_MAX,
    BATCH_SIZE,
    ONE_LINE_MAX,
    PEOPLE_MAX,
    PERSON_MAX,
    Category,
    Importance,
    TriageBatch,
    TriageRecord,
    assign_aliases,
    batches,
    sanitize_record,
    sanitize_text,
    stub_record,
    validate_batch,
)

SOURCE = "gmail_test"
MOMENT = datetime(2026, 9, 1, 12, 0, 0)


def make_item(
    external_id: str = "a1",
    subject: str = "Quarterly planning",
    sender: str = "Alex Example <alex@example.com>",
    bulk: bool = False,
) -> NormalizedItem:
    payload: dict[str, Any] = {"subject": subject, "from": sender, "bodyText": "Hello.", "labelIds": ["INBOX"]}
    if bulk:
        payload["listUnsubscribe"] = "<mailto:unsubscribe@example.com>"
    return normalize_gmail(SOURCE, external_id, MOMENT, payload)


def make_items(count: int) -> list[NormalizedItem]:
    return [make_item(external_id=f"id{n}") for n in range(count)]


def make_record(alias: str = "m001", **overrides: Any) -> TriageRecord:
    fields: dict[str, Any] = {
        "id": alias,
        "category": Category.FYI,
        "importance": Importance.NORMAL,
        "one_line": "Planning agenda circulated.",
        "action_needed": False,
        "action_text": None,
        "due": None,
        "people": ["Alex Example"],
    }
    fields.update(overrides)
    return TriageRecord(**fields)


# --- assign_aliases --------------------------------------------------------------------------------


def test_assign_aliases_follows_input_order_and_pads_to_three_digits():
    items = make_items(3)

    pairs, _ = assign_aliases(items)

    assert [alias for alias, _ in pairs] == ["m001", "m002", "m003"]
    assert [item for _, item in pairs] == items


def test_assign_aliases_maps_alias_to_real_item_id():
    items = make_items(2)

    _, mapping = assign_aliases(items)

    assert mapping == {"m001": "gmail_test:id0", "m002": "gmail_test:id1"}


def test_assign_aliases_widens_past_999():
    pairs, mapping = assign_aliases(make_items(1001))

    aliases = [alias for alias, _ in pairs]
    assert aliases[998] == "m999"
    assert aliases[999] == "m1000"
    assert aliases[1000] == "m1001"
    assert len(set(aliases)) == 1001
    assert mapping["m1000"] == "gmail_test:id999"


def test_assign_aliases_is_deterministic_and_per_call():
    items = make_items(3)

    first = assign_aliases(items)
    second = assign_aliases(items)
    reordered = assign_aliases(list(reversed(items)))

    assert first == second
    assert reordered[1]["m001"] == "gmail_test:id2"


def test_assign_aliases_empty_input_gives_nothing():
    assert assign_aliases([]) == ([], {})


# --- batches ---------------------------------------------------------------------------------------


def test_batches_splits_in_order_with_remainder():
    assert list(batches([1, 2, 3, 4, 5], size=2)) == [[1, 2], [3, 4], [5]]


def test_batches_exact_multiple_has_no_empty_tail():
    assert list(batches([1, 2, 3, 4], size=2)) == [[1, 2], [3, 4]]


def test_batches_empty_input_yields_nothing():
    assert list(batches([], size=3)) == []


def test_batches_default_size_is_batch_size():
    chunks = list(batches(list(range(BATCH_SIZE + 1))))

    assert [len(chunk) for chunk in chunks] == [BATCH_SIZE, 1]


@pytest.mark.parametrize("size", [0, -1])
def test_batches_rejects_size_below_one_at_call_time(size: int):
    with pytest.raises(ValueError):
        batches([1, 2, 3], size=size)


# --- validate_batch --------------------------------------------------------------------------------


def test_validate_batch_drops_unknown_aliases():
    result = validate_batch([make_record("m001"), make_record("m999")], ["m001", "m002"])

    assert [r.id for r in result.valid] == ["m001"]
    assert result.unknown == ["m999"]


def test_validate_batch_duplicates_keep_first_record():
    first = make_record("m001", one_line="First.")
    second = make_record("m001", one_line="Second.")

    result = validate_batch([first, second], ["m001"])

    assert [r.one_line for r in result.valid] == ["First."]
    assert result.duplicates == ["m001"]


def test_validate_batch_reports_missing_aliases_for_retry():
    result = validate_batch([make_record("m002")], ["m001", "m002", "m003"])

    assert result.missing == ["m001", "m003"]
    assert [r.id for r in result.valid] == ["m002"]


def test_validate_batch_output_order_follows_sent_aliases():
    records = [make_record("m003"), make_record("m001"), make_record("m002")]

    result = validate_batch(records, ["m001", "m002", "m003"])

    assert [r.id for r in result.valid] == ["m001", "m002", "m003"]


def test_validate_batch_sanitizes_url_out_of_one_line():
    record = make_record("m001", one_line="Read this https://evil.example.com/x?a=1 now")

    result = validate_batch([record], ["m001"])

    assert result.valid[0].one_line == "Read this now"


def test_validate_batch_complete_output_reports_nothing_missing():
    result = validate_batch([make_record("m001")], ["m001"])

    assert (result.missing, result.unknown, result.duplicates) == ([], [], [])


# --- sanitize_text ---------------------------------------------------------------------------------


def test_sanitize_text_removes_http_and_https_urls():
    assert sanitize_text("see http://a.example.com and https://b.example.com/p?q=1 ok", 100) == "see and ok"


def test_sanitize_text_removes_www_urls():
    assert sanitize_text("visit www.example.com/page today", 100) == "visit today"


def test_sanitize_text_markdown_link_keeps_only_label():
    assert sanitize_text("open [the doc](https://example.com/d) please", 100) == "open the doc please"


def test_sanitize_text_markdown_link_with_url_label_leaves_no_url():
    assert sanitize_text("[https://example.com](https://example.com)", 100) == ""


def test_sanitize_text_strips_html_tags():
    assert sanitize_text("<b>Bold</b> and <a href='https://x.example.com'>link</a>", 100) == "Bold and link"


def test_sanitize_text_tag_split_url_does_not_reassemble():
    assert "https://" not in sanitize_text("ht<b></b>tps://evil.example.com", 100)


def test_sanitize_text_keeps_bare_angle_brackets_in_prose():
    assert sanitize_text("if a < b and c > d", 100) == "if a < b and c > d"


def test_sanitize_text_replaces_control_characters_with_space():
    assert sanitize_text("a\x00b\x07c\x1bd\x7fe", 100) == "a b c d e"


def test_sanitize_text_collapses_whitespace():
    assert sanitize_text("  a \t b\n\n c  ", 100) == "a b c"


def test_sanitize_text_cuts_at_whitespace_boundary():
    assert sanitize_text("alpha beta gamma", 12) == "alpha beta"


def test_sanitize_text_hard_cuts_a_single_long_token():
    assert sanitize_text("x" * 50, 10) == "x" * 10


def test_sanitize_text_short_text_is_untouched():
    assert sanitize_text("fits", 100) == "fits"


def test_sanitize_text_empty_and_markup_only_give_empty_string():
    assert sanitize_text("", 10) == ""
    assert sanitize_text("<br/> https://example.com", 10) == ""


# --- sanitize_record -------------------------------------------------------------------------------


@pytest.mark.parametrize("due", ["tomorrow", "2026-13-45", "2026-02-30", "20260920", "2026-9-20", "", "2026-W38-1"])
def test_sanitize_record_invalid_due_becomes_none(due: str):
    assert sanitize_record(make_record(due=due)).due is None


def test_sanitize_record_valid_due_is_kept():
    assert sanitize_record(make_record(due="2026-09-30")).due == "2026-09-30"


def test_sanitize_record_null_due_stays_none():
    assert sanitize_record(make_record(due=None)).due is None


def test_sanitize_record_empty_action_text_becomes_none():
    record = make_record(action_needed=True, action_text="  <br> https://example.com  ")

    cleaned = sanitize_record(record)

    assert cleaned.action_text is None
    assert cleaned.action_needed is True


def test_sanitize_record_action_text_is_sanitized_and_capped():
    cleaned = sanitize_record(make_record(action_text="Reply to www.example.com " + "word " * 200))

    assert cleaned.action_text is not None
    assert "www." not in cleaned.action_text
    assert len(cleaned.action_text) <= ACTION_TEXT_MAX


def test_sanitize_record_caps_one_line():
    assert len(sanitize_record(make_record(one_line="word " * 200)).one_line) <= ONE_LINE_MAX


def test_sanitize_record_people_dedupe_preserving_order_and_drop_empties():
    cleaned = sanitize_record(make_record(people=["Bo", "", "  ", "Al", "Bo", "<i>Al</i>", "Cy"]))

    assert cleaned.people == ["Bo", "Al", "Cy"]


def test_sanitize_record_people_capped_in_count_and_length():
    people = [f"Person{n}" for n in range(PEOPLE_MAX + 5)]
    cleaned = sanitize_record(make_record(people=people + ["n" * (PERSON_MAX * 2)]))

    assert len(cleaned.people) == PEOPLE_MAX
    assert cleaned.people == people[:PEOPLE_MAX]

    long_only = sanitize_record(make_record(people=["n" * (PERSON_MAX * 2)]))
    assert len(long_only.people[0]) == PERSON_MAX


def test_sanitize_record_preserves_id_and_enums():
    cleaned = sanitize_record(make_record("m007", category=Category.MEETING, importance=Importance.HIGH))

    assert (cleaned.id, cleaned.category, cleaned.importance) == ("m007", Category.MEETING, Importance.HIGH)


# --- stub_record -----------------------------------------------------------------------------------


def test_stub_record_non_bulk_is_normal_importance_with_sender_in_line():
    record = stub_record("m001", make_item(subject="Quarterly planning"))

    assert record == TriageRecord(
        id="m001",
        category=Category.OTHER,
        importance=Importance.NORMAL,
        one_line="Quarterly planning (from Alex Example)",
        action_needed=False,
        action_text=None,
        due=None,
        people=["Alex Example"],
    )


def test_stub_record_bulk_is_low_importance():
    assert stub_record("m001", make_item(bulk=True)).importance is Importance.LOW


def test_stub_record_uses_address_when_sender_has_no_name():
    record = stub_record("m001", make_item(sender="alex@example.com"))

    assert record.one_line.endswith("(from alex@example.com)")
    assert record.people == ["alex@example.com"]


def test_stub_record_without_participants_has_bare_title_and_no_people():
    item = make_item().model_copy(update={"participants": []})

    record = stub_record("m001", item)

    assert record.one_line == "Quarterly planning"
    assert record.people == []


def test_stub_record_ignores_non_sender_participants():
    recipient = Participant(name="Sam Sample", address="sam@example.org", role="to")
    item = make_item().model_copy(update={"participants": [recipient]})

    record = stub_record("m001", item)

    assert record.people == []
    assert record.one_line == "Quarterly planning"


def test_stub_record_empty_title_and_no_participants_is_accepted():
    item = make_item().model_copy(update={"title": "", "participants": []})

    record = stub_record("m001", item)

    assert record.one_line == NO_SUBJECT
    assert record.people == []


def test_stub_record_empty_title_keeps_sender():
    item = make_item().model_copy(update={"title": ""})

    assert stub_record("m001", item).one_line == f"{NO_SUBJECT} (from Alex Example)"


def test_stub_record_strips_links_from_title_and_caps_length():
    item = make_item(subject="Look https://evil.example.com " + "word " * 100)

    record = stub_record("m001", item)

    assert "https://" not in record.one_line
    assert len(record.one_line) <= ONE_LINE_MAX


def test_stub_record_is_deterministic():
    item = make_item()

    assert stub_record("m001", item) == stub_record("m001", item)


# --- schema ----------------------------------------------------------------------------------------

FORBIDDEN_KEYS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength"}


def walk_keys(node: object) -> set[str]:
    if isinstance(node, dict):
        keys = set(node)
        for value in node.values():
            keys |= walk_keys(value)
        return keys
    if isinstance(node, list):
        keys: set[str] = set()
        for value in node:
            keys |= walk_keys(value)
        return keys
    return set()


def refs_in(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found |= refs_in(value)
    elif isinstance(node, list):
        for value in node:
            found |= refs_in(value)
    return found


def reaches_itself(defs: dict[str, Any], name: str) -> bool:
    seen: set[str] = set()
    stack = list(refs_in(defs[name]))
    while stack:
        current = stack.pop()
        if current == name:
            return True
        if current in seen or current not in defs:
            continue
        seen.add(current)
        stack.extend(refs_in(defs[current]))
    return False


def test_triage_schema_has_no_numeric_or_length_constraints():
    schema = TriageBatch.model_json_schema()

    assert walk_keys(schema) & FORBIDDEN_KEYS == set()


def test_triage_schema_has_no_recursive_refs():
    schema = TriageBatch.model_json_schema()
    defs = schema.get("$defs", {})

    assert defs
    assert [name for name in defs if reaches_itself(defs, name)] == []


def test_triage_schema_requires_every_record_field():
    schema = TriageBatch.model_json_schema()

    record = schema["$defs"]["TriageRecord"]
    assert set(record["required"]) == set(record["properties"])
    assert record["additionalProperties"] is False


def test_triage_record_rejects_extra_field():
    with pytest.raises(ValidationError):
        make_record(confidence=0.9)


def test_triage_record_rejects_unknown_category():
    with pytest.raises(ValidationError):
        make_record(category="urgent")


def test_triage_record_rejects_unknown_importance():
    with pytest.raises(ValidationError):
        make_record(importance="critical")


def test_triage_batch_rejects_extra_field():
    with pytest.raises(ValidationError):
        TriageBatch.model_validate({"records": [], "note": "hi"})


def test_triage_record_requires_nullable_fields_to_be_present():
    missing_due: dict[str, Optional[object]] = {
        "id": "m001",
        "category": "fyi",
        "importance": "low",
        "one_line": "x",
        "action_needed": False,
        "action_text": None,
        "people": [],
    }

    with pytest.raises(ValidationError):
        TriageRecord.model_validate(missing_due)
