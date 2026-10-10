"""Tests for pipeline.packages settings and status model."""
import logging

from pipeline import health
from pipeline.packages import (
    STATUS_RANK, STATUSES, TERMINAL_STATUSES, PackagesSettings, packages_settings,
)


def test_defaults():
    s = packages_settings()
    assert s.refresh_cron == "23 */2 * * *"
    assert (s.scan_days, s.stale_days) == (60, 30)
    assert (s.poll_interval_hours, s.stop_after_delivered_hours) == (2, 48)
    assert not s.ups_enabled and not s.usps_enabled


def test_overrides():
    with health.env_override({
        "PACKAGES_REFRESH_CRON": "5 * * * *", "PACKAGES_SCAN_DAYS": "90",
        "PACKAGES_STALE_DAYS": "14",
    }):
        s = packages_settings()
    assert (s.refresh_cron, s.scan_days, s.stale_days) == ("5 * * * *", 90, 14)


def test_malformed_fall_back_with_warning(caplog):
    with health.env_override({
        "PACKAGES_REFRESH_CRON": "not a cron", "PACKAGES_SCAN_DAYS": "abc",
        "PACKAGES_STALE_DAYS": "0",
    }), caplog.at_level(logging.WARNING):
        s = packages_settings()
    assert (s.refresh_cron, s.scan_days, s.stale_days) == ("23 */2 * * *", 60, 30)
    assert "PACKAGES_SCAN_DAYS" in caplog.text
    assert "PACKAGES_STALE_DAYS" in caplog.text
    assert "PACKAGES_REFRESH_CRON" in caplog.text


def test_scan_days_upper_bound():
    with health.env_override({"PACKAGES_SCAN_DAYS": "366"}):
        assert packages_settings().scan_days == 60


def test_enabled_flags():
    with health.env_override({"UPS_CLIENT_ID": "a", "UPS_CLIENT_SECRET": "b", "USPS_CLIENT_ID": "c"}):
        s = packages_settings()
    assert s.ups_enabled and not s.usps_enabled
    with health.env_override({"UPS_CLIENT_ID": "  ", "UPS_CLIENT_SECRET": "b"}):
        s = packages_settings()
    assert s.ups_client_id is None and not s.ups_enabled


def test_secrets_not_in_repr():
    s = PackagesSettings(ups_client_id="idvalue1", ups_client_secret="secretvalue2",
                         usps_client_id="idvalue3", usps_client_secret="secretvalue4")
    text = repr(s)
    for leaked in ("idvalue1", "secretvalue2", "idvalue3", "secretvalue4"):
        assert leaked not in text


def test_status_ranking():
    assert STATUS_RANK["pending"] < STATUS_RANK["in_transit"] < STATUS_RANK["out_for_delivery"] < STATUS_RANK["delivered"]
    assert TERMINAL_STATUSES == {"delivered", "returned"}
    assert set(STATUS_RANK) <= set(STATUSES)
    assert "exception" in STATUSES and "exception" not in STATUS_RANK


def test_settings_read_from_process_environment_without_env_file(monkeypatch):
    """In the container compose's env_file reaches the app only as environment variables."""
    from pipeline import health
    from pipeline.packages import packages_settings

    monkeypatch.setattr(health, "ROOT", health.ROOT / "nonexistent-root-for-test")
    monkeypatch.setenv("PACKAGES_SCAN_DAYS", "90")
    monkeypatch.setenv("UPS_CLIENT_ID", "id-from-process-env")
    monkeypatch.setenv("UPS_CLIENT_SECRET", "secret-from-process-env")
    settings = packages_settings()
    assert settings.scan_days == 90
    assert settings.ups_enabled
