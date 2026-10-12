"""Tests for the cached pipeline.packages.carriers.base.adapters() factory."""
import pytest

from pipeline.packages.carriers import base


@pytest.fixture(autouse=True)
def clean_cache():
    base._adapter_cache.clear()
    yield
    base._adapter_cache.clear()


def test_no_keys_returns_empty():
    assert base.adapters() == {}


def test_same_instance_across_calls(monkeypatch):
    monkeypatch.setenv("UPS_CLIENT_ID", "id")
    monkeypatch.setenv("UPS_CLIENT_SECRET", "secret")
    first = base.adapters()
    second = base.adapters()
    assert set(first) == {"ups"}
    assert first["ups"] is second["ups"]


def test_new_instance_after_credentials_change(monkeypatch):
    monkeypatch.setenv("UPS_CLIENT_ID", "id")
    monkeypatch.setenv("UPS_CLIENT_SECRET", "secret")
    old = base.adapters()["ups"]
    monkeypatch.setenv("UPS_CLIENT_SECRET", "rotated")
    new = base.adapters()["ups"]
    assert new is not old
    assert base.adapters()["ups"] is new


def test_usps_cached_independently(monkeypatch):
    monkeypatch.setenv("USPS_CLIENT_ID", "id")
    monkeypatch.setenv("USPS_CLIENT_SECRET", "secret")
    found = base.adapters()
    assert set(found) == {"usps"}
    assert base.adapters()["usps"] is found["usps"]
