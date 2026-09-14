"""Thin CLI wrapper around pipeline.health.check_zoom."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.health import check_zoom  # noqa: E402

if __name__ == "__main__":
    result = check_zoom()
    print(f"[zoom] {result.status.upper()} — {result.detail}")
    sys.exit(0 if result.status == "ok" else 1)
