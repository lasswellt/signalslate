"""
Tests that pipeline.runner scrubs credentials from every free text it persists: Run.error,
Run.summary, SourceHealth.detail and the per-source last_detail, and never lets the scrubber itself
break a run (docs/_research/2026-09-21_management-ui.md section 2).

The connection store, the vault and pipeline.redact are real. Only the network edge (the health check
and the collector call) is stubbed. The stored secret is deliberately not shaped like any token, so a
pass can only mean it was scrubbed by value.
"""
import sys
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config_store, connections, crypto, db, health, runner  # noqa: E402
from pipeline.collectors import CollectionResult  # noqa: E402
from pipeline.health import HealthResult  # noqa: E402

STORED_SECRET = "slack-token-Hd91XvB7mR"
BEARER = "Bearer abcdefghijklmnop0123456789"
BEARER_TOKEN = "abcdefghijklmnop0123456789"
QUERY_SECRET = "qsecretValue987654"
QUERY_FRAGMENT = f"client_secret={QUERY_SECRET}"
LEAKS = (STORED_SECRET, BEARER_TOKEN, QUERY_SECRET)

LEAKY = f"denied for {STORED_SECRET}: Authorization {BEARER} at /oauth?{QUERY_FRAGMENT}&x=1"


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def declared(monkeypatch, tmp_path):
    monkeypatch.setattr(health, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("SLACK_ALPHA_TOKEN=xoxp-a\nSLACK_BETA_TOKEN=xoxp-b\n")
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "data" / "config.json")


@pytest.fixture
def vault(temp_db):
    v = crypto.Vault([crypto.generate_key()])
    connections.set_vault(v)
    connections.create("slack", {"label": "work", "token": STORED_SECRET})
    yield v
    connections.set_vault(None)


def stub_network(monkeypatch, outcome, health_detail=""):
    """Every requested source is healthy (or reports `health_detail` as an error) and collects `outcome`."""

    def check(active):
        status = "error" if health_detail else "ok"
        return [HealthResult(s, status, health_detail) for s, on in active.items() if on]

    def collect(source, _until):
        if isinstance(outcome, Exception):
            raise outcome
        return CollectionResult(source, outcome[0], outcome[1])

    monkeypatch.setattr(runner, "check_all_configured", check)
    monkeypatch.setattr(runner, "_collect_source", collect)


def persisted_texts(run) -> list[str]:
    """Every free text the runner wrote for this run, including the per-source attempt detail."""
    texts = [run.error or "", run.summary or ""]
    with db.get_session() as session:
        texts += [h.detail or "" for h in session.exec(select(db.SourceHealth)).all()]
        texts += [c.last_detail or "" for c in session.exec(select(db.SourceCursor)).all()]
    return texts


def assert_clean(run) -> None:
    for text in persisted_texts(run):
        for leak in LEAKS:
            assert leak not in text


def test_returned_collector_detail_is_scrubbed_everywhere(temp_db, declared, vault, monkeypatch):
    stub_network(monkeypatch, ("error", LEAKY))

    run = runner.execute_run(only="slack_alpha")

    assert run.status == "failed"
    assert run.error is not None and "[redacted]" in run.error
    assert "denied for" in run.error  # the diagnostic survives around the secrets
    assert_clean(run)


def test_health_detail_is_scrubbed(temp_db, declared, vault, monkeypatch):
    stub_network(monkeypatch, ("ok", "fine"), health_detail=LEAKY)

    run = runner.execute_run(only="slack_alpha")

    assert run.id is not None
    rows = db.source_health_for_run(run.id)
    assert len(rows) == 1 and rows[0].detail is not None
    assert "[redacted]" in rows[0].detail
    assert_clean(run)


def test_raised_exception_message_is_scrubbed(temp_db, declared, vault, monkeypatch):
    stub_network(monkeypatch, RuntimeError(LEAKY))

    run = runner.execute_run(only="slack_alpha")

    assert run.error is not None
    assert run.error.startswith("slack_alpha: RuntimeError: ")
    assert "[redacted]" in run.error
    assert_clean(run)


def test_unreadable_store_falls_back_to_pattern_only(temp_db, declared, vault, monkeypatch):
    def boom():
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(connections, "secret_values", boom)
    stub_network(monkeypatch, ("error", LEAKY))

    run = runner.execute_run(only="slack_alpha")

    assert run.status == "failed"
    assert run.error is not None
    # Shapes still go; the stored value is not known, so it is the only thing that may remain.
    assert BEARER_TOKEN not in run.error and QUERY_SECRET not in run.error
    assert "denied for" in run.error


def test_redact_failing_with_secrets_retries_pattern_only(temp_db, declared, vault, monkeypatch):
    real = runner.redact

    def flaky(text, secrets=(), **kwargs):
        if list(secrets):
            raise RuntimeError("scrubber broke")
        return real(text, secrets, **kwargs)

    monkeypatch.setattr(runner, "redact", flaky)
    stub_network(monkeypatch, ("error", LEAKY))

    run = runner.execute_run(only="slack_alpha")

    assert run.status == "failed"
    assert run.error is not None
    assert BEARER_TOKEN not in run.error and QUERY_SECRET not in run.error
    assert "denied for" in run.error


def test_redact_raising_never_blocks_the_run_or_leaks(temp_db, declared, vault, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("scrubber broke")

    monkeypatch.setattr(runner, "redact", boom)
    stub_network(monkeypatch, ("error", LEAKY))

    run = runner.execute_run(only="slack_alpha")

    assert run.status == "failed"  # finalised, not left at "running"
    assert run.finished_at is not None
    assert run.summary
    assert_clean(run)


def test_long_texts_are_capped_after_redaction(temp_db, declared, vault, monkeypatch):
    long_text = "x" * 5000 + f" {STORED_SECRET}"
    stub_network(monkeypatch, ("error", long_text), health_detail=long_text)

    health_run = runner.execute_run(only="slack_alpha")
    assert health_run.id is not None
    assert all(len(h.detail or "") <= runner.MAX_PERSISTED_CHARS for h in db.source_health_for_run(health_run.id))

    stub_network(monkeypatch, ("error", long_text))
    run = runner.execute_run(only="slack_alpha")

    assert run.error is not None
    assert 0 < len(run.error) <= runner.MAX_PERSISTED_CHARS
    assert len(run.summary or "") <= runner.MAX_PERSISTED_CHARS
    assert_clean(run)


def test_scrub_passes_none_through_and_never_raises(monkeypatch):
    assert runner._scrub(None) is None

    def boom(*_args, **_kwargs):
        raise RuntimeError("scrubber broke")

    monkeypatch.setattr(runner, "redact", boom)
    assert runner._scrub(LEAKY, []) == "[redacted]"
