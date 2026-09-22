"""
Unit tests for pipeline.jobs (jobs_settings, slug_candidates, normalize_title/city, content_hash).

jobs_settings() reads through pipeline.health.env(), which merges ROOT/".env" with the process
environment: like tests/test_domain_base.py, each config test points health.ROOT at a tmp_path
holding a purpose-built .env, since JOBS_ is not one of health._raw_env()'s process-env prefixes.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import health, jobs  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Writes a .env into tmp_path and points health.ROOT at it, mirroring tests/test_domain_base.py."""

    def _write(contents: str) -> Path:
        (tmp_path / ".env").write_text(contents)
        monkeypatch.setattr(health, "ROOT", tmp_path)
        return tmp_path

    return _write


# --- jobs_settings -------------------------------------------------------------------


def test_jobs_settings_defaults(env):
    env("")
    settings = jobs.jobs_settings()
    assert settings.refresh_cron == "0 6 * * *"
    assert settings.score_threshold == 0.6
    assert settings.missed_polls_to_close == 3
    assert settings.max_llm_resolves_per_run == 10
    assert settings.max_score_per_run == 50


def test_jobs_settings_reads_explicit_values(env):
    env(
        "JOBS_REFRESH_CRON=0 7 * * *\n"
        "JOBS_SCORE_THRESHOLD=0.75\n"
        "JOBS_MISSED_POLLS_TO_CLOSE=5\n"
        "JOBS_MAX_LLM_RESOLVES_PER_RUN=20\n"
        "JOBS_MAX_SCORE_PER_RUN=100\n"
    )
    settings = jobs.jobs_settings()
    assert settings.refresh_cron == "0 7 * * *"
    assert settings.score_threshold == 0.75
    assert settings.missed_polls_to_close == 5
    assert settings.max_llm_resolves_per_run == 20
    assert settings.max_score_per_run == 100


def test_jobs_settings_invalid_score_threshold_fails_closed(env):
    env("JOBS_SCORE_THRESHOLD=not-a-number\n")
    settings = jobs.jobs_settings()
    assert settings.score_threshold == 0.6


def test_jobs_settings_invalid_missed_polls_fails_closed(env):
    env("JOBS_MISSED_POLLS_TO_CLOSE=lots\n")
    settings = jobs.jobs_settings()
    assert settings.missed_polls_to_close == 3


def test_jobs_settings_invalid_max_llm_resolves_fails_closed(env):
    env("JOBS_MAX_LLM_RESOLVES_PER_RUN=abc\n")
    settings = jobs.jobs_settings()
    assert settings.max_llm_resolves_per_run == 10


def test_jobs_settings_invalid_max_score_per_run_fails_closed(env):
    env("JOBS_MAX_SCORE_PER_RUN=abc\n")
    settings = jobs.jobs_settings()
    assert settings.max_score_per_run == 50


# --- slug_candidates -------------------------------------------------------------------


def test_slug_candidates_strips_suffix_and_dedupes_against_domain():
    assert jobs.slug_candidates("Acme Inc", "acme.com") == ["acme", "acmeinc"]


def test_slug_candidates_no_suffix_no_domain():
    assert jobs.slug_candidates("Widgetco") == ["widgetco"]


def test_slug_candidates_caps_at_three():
    candidates = jobs.slug_candidates("Big Umbrella Holdings Corp", "bigumbrella.io")
    assert len(candidates) <= 3
    assert candidates[0] == "bigumbrella"


def test_slug_candidates_handles_missing_domain():
    assert jobs.slug_candidates("Acme Inc", None) == ["acme", "acmeinc"]


def test_slug_candidates_handles_empty_name():
    assert jobs.slug_candidates("", "acme.com") == ["acme"]


def test_slug_candidates_lowercases_and_strips_punctuation():
    assert jobs.slug_candidates("Acme, LLC.") == ["acme", "acmellc"]


# --- normalize_title / normalize_city -------------------------------------------------


def test_normalize_title_casing_and_whitespace():
    assert jobs.normalize_title("  Senior  Software Engineer  ") == "senior software engineer"
    assert jobs.normalize_title("Senior Software Engineer") == "senior software engineer"


def test_normalize_title_punctuation_variance():
    assert jobs.normalize_title("Software Engineer II") == jobs.normalize_title("software-engineer-ii")


def test_normalize_city_casing_whitespace_and_punctuation():
    assert jobs.normalize_city("  St. Louis ") == jobs.normalize_city("St Louis")
    assert jobs.normalize_city("Remote - US") == jobs.normalize_city("Remote, US")


def test_normalize_city_empty_is_empty_string():
    assert jobs.normalize_city(None) == ""
    assert jobs.normalize_city("") == ""


# --- content_hash -------------------------------------------------------------------


def test_content_hash_stable_across_description_whitespace_variance():
    a = jobs.content_hash("Engineer", "Line one.\nLine two.", apply_url="https://x.com/job/1")
    b = jobs.content_hash("Engineer", "Line one.   Line two.", apply_url="https://x.com/job/1")
    assert a == b


def test_content_hash_stable_across_tracking_params():
    a = jobs.content_hash("Engineer", "desc", apply_url="https://x.com/job/1?id=5")
    b = jobs.content_hash(
        "Engineer", "desc", apply_url="https://x.com/job/1?id=5&utm_source=newsletter&utm_medium=email"
    )
    assert a == b


def test_content_hash_changes_when_meaningful_field_changes():
    a = jobs.content_hash("Engineer", "desc", apply_url="https://x.com/job/1")
    b = jobs.content_hash("Engineer", "a different description", apply_url="https://x.com/job/1")
    assert a != b


def test_content_hash_changes_when_query_param_is_not_tracking():
    a = jobs.content_hash("Engineer", "desc", apply_url="https://x.com/job/1?id=5")
    b = jobs.content_hash("Engineer", "desc", apply_url="https://x.com/job/1?id=6")
    assert a != b


def test_content_hash_is_a_hex_sha256_digest():
    digest = jobs.content_hash("Engineer", "desc")
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex
