"""
Unit tests for pipeline.domains (normalize_domain, domain_settings) and
pipeline.domains.registrars (the typed dataclasses, the Registrar Protocol, the typed errors).

domain_settings() reads through pipeline.health.env(), which merges ROOT/".env" with the process
environment: like tests/test_health.py, each config test points health.ROOT at a tmp_path holding
a purpose-built .env, since DOMAINS_ is not one of health._raw_env()'s process-env prefixes.
"""
import dataclasses
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import connections, domains, health  # noqa: E402
from pipeline.domains import registrars  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Writes a .env into tmp_path and points health.ROOT at it, mirroring tests/test_health.py."""

    def _write(contents: str) -> Path:
        (tmp_path / ".env").write_text(contents)
        monkeypatch.setattr(health, "ROOT", tmp_path)
        return tmp_path

    return _write


# --- normalize_domain --------------------------------------------------------------


def test_normalize_domain_strips_lowercases_and_drops_trailing_dot():
    assert domains.normalize_domain("  Example.COM.  ") == ("example.com", "com")


def test_normalize_domain_idna_encodes_unicode_to_punycode():
    assert domains.normalize_domain("café.de") == ("xn--caf-dma.de", "de")


def test_normalize_domain_passes_through_existing_punycode():
    assert domains.normalize_domain("XN--CAF-DMA.DE") == ("xn--caf-dma.de", "de")


def test_normalize_domain_rejects_empty():
    with pytest.raises(ValueError):
        domains.normalize_domain("")
    with pytest.raises(ValueError):
        domains.normalize_domain("   ")


def test_normalize_domain_rejects_scheme():
    with pytest.raises(ValueError):
        domains.normalize_domain("https://example.com")


def test_normalize_domain_rejects_path():
    with pytest.raises(ValueError):
        domains.normalize_domain("example.com/path")


def test_normalize_domain_rejects_missing_tld():
    with pytest.raises(ValueError):
        domains.normalize_domain("localhost")


def test_normalize_domain_rejects_label_over_63():
    with pytest.raises(ValueError):
        domains.normalize_domain("a" * 64 + ".com")


def test_normalize_domain_accepts_label_at_63():
    name, tld = domains.normalize_domain("a" * 63 + ".com")
    assert name == "a" * 63 + ".com"
    assert tld == "com"


def test_normalize_domain_rejects_total_over_253():
    # Five 50-char labels + a short TLD comfortably clears 253 once dots are counted.
    long_name = ".".join(["a" * 50] * 5) + ".co"
    with pytest.raises(ValueError):
        domains.normalize_domain(long_name)


# --- domain_settings ---------------------------------------------------------------


def test_domain_settings_defaults(env):
    env("")
    settings = domains.domain_settings()
    assert settings.purchase_enabled is False
    assert settings.max_price == Decimal("25")
    assert settings.daily_cap == Decimal("50")
    assert settings.allow_premium is False
    assert settings.refresh_cron == "30 5 * * *"
    assert settings.resolver is None


def test_domain_settings_reads_explicit_values(env):
    env(
        "DOMAINS_PURCHASE_ENABLED=true\n"
        "DOMAINS_MAX_PRICE=15.50\n"
        "DOMAINS_DAILY_CAP=100\n"
        "DOMAINS_ALLOW_PREMIUM=true\n"
        "DOMAINS_REFRESH_CRON=0 6 * * *\n"
        "DOMAINS_RESOLVER=1.1.1.1\n"
    )
    settings = domains.domain_settings()
    assert settings.purchase_enabled is True
    assert settings.max_price == Decimal("15.50")
    assert settings.daily_cap == Decimal("100")
    assert settings.allow_premium is True
    assert settings.refresh_cron == "0 6 * * *"
    assert settings.resolver == "1.1.1.1"


def test_domain_settings_invalid_max_price_fails_closed(env):
    env("DOMAINS_PURCHASE_ENABLED=true\nDOMAINS_MAX_PRICE=not-a-number\n")
    settings = domains.domain_settings()
    assert settings.purchase_enabled is False
    assert settings.max_price == Decimal("25")


def test_domain_settings_invalid_daily_cap_fails_closed(env):
    env("DOMAINS_PURCHASE_ENABLED=true\nDOMAINS_DAILY_CAP=lots\n")
    settings = domains.domain_settings()
    assert settings.purchase_enabled is False
    assert settings.daily_cap == Decimal("50")


# --- registrars: dataclasses, Protocol shape, typed errors -------------------------


def test_registrar_domain_and_quote_use_decimal_prices():
    quote = registrars.Quote(
        name="example.com",
        available=True,
        premium=False,
        price=Decimal("12.98"),
        renewal_price=Decimal("14.98"),
        currency="USD",
    )
    assert isinstance(quote.price, Decimal)
    assert isinstance(quote.renewal_price, Decimal)

    domain = registrars.RegistrarDomain(
        name="example.com",
        expires_at=None,
        auto_renew=True,
        locked=True,
        privacy=None,
        nameservers=("ns1.example.com", "ns2.example.com"),
    )
    assert domain.nameservers == ("ns1.example.com", "ns2.example.com")


def test_dataclasses_are_frozen():
    contact = registrars.Contact(
        first_name="A", last_name="B", address1="1 Main St", city="Town",
        state_province="ST", postal_code="00000", country="US", phone="+1.5555555555",
        email="a@example.com",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        contact.email = "other@example.com"  # type: ignore[misc]


def test_contact_fields_match_connections_registrant_contact_keys():
    """Contact must accept exactly what connections.py validates as registrant_contact."""
    contact_fields = {f.name for f in dataclasses.fields(registrars.Contact)}
    assert contact_fields == set(connections._REGISTRANT_CONTACT_KEYS)


def test_purchase_result_shape():
    result = registrars.PurchaseResult(
        name="example.com", success=True, order_id="ord-1",
        charged=Decimal("12.98"), detail="purchased",
    )
    assert result.success is True
    assert result.charged == Decimal("12.98")


def test_registrar_is_a_protocol():
    assert getattr(registrars.Registrar, "_is_protocol", False) is True


@pytest.mark.parametrize(
    "error_cls",
    [
        registrars.NotEligible,
        registrars.IpNotWhitelisted,
        registrars.RateLimited,
        registrars.AuthFailed,
        registrars.Unsupported,
    ],
)
def test_typed_errors_are_registrar_errors(error_cls):
    assert issubclass(error_cls, registrars.RegistrarError)
    assert issubclass(registrars.RegistrarError, Exception)
    with pytest.raises(registrars.RegistrarError):
        raise error_cls("detail")
