"""Sanity tests for the vendored jkeen tracking_number_data courier JSON."""
import json
import re
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parent.parent / "pipeline" / "packages" / "data"
FILES = sorted((DATA / "couriers").glob("*.json"))


def _to_python(pattern: str) -> str:
    """Convert Ruby/JS named groups to Python syntax."""
    return re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", pattern)


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile; on duplicate group names, rename later duplicates uniquely."""
    py = _to_python(pattern)
    seen: dict[str, int] = {}

    def rename(m: re.Match[str]) -> str:
        n = m.group(1)
        seen[n] = seen.get(n, 0) + 1
        return f"(?P<{n}>" if seen[n] == 1 else f"(?P<{n}__{seen[n]}>"

    return re.compile(re.sub(r"\(\?P<(\w+)>", rename, py))


def _joined(regex: object) -> str:
    return "".join(regex) if isinstance(regex, list) else str(regex)


def test_vendor_files_present() -> None:
    assert (DATA / "LICENSE").read_text().find("MIT") >= 0
    assert "Commit:" in (DATA / "SOURCE.md").read_text()
    assert {"ups", "usps", "fedex"} <= {p.stem for p in FILES}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_courier_schema_and_regex(path: Path) -> None:
    doc = json.loads(path.read_text())
    assert doc.get("name")
    assert doc.get("courier_code")
    entries = doc.get("tracking_numbers")
    assert isinstance(entries, list) and entries
    for entry in entries:
        assert entry.get("regex"), entry.get("name")
        _compile(_joined(entry["regex"]))
        if "test_numbers" in entry and "valid" in entry["test_numbers"]:
            assert entry["test_numbers"]["valid"], entry.get("name")
