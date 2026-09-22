"""
Unit tests for pipeline.domains.dns (lookup_records, mail_posture).

No network: a FakeResolver stands in for dns.resolver.Resolver (a true external), and the MTA-STS
policy fetch stubs dns.py's own `requests.get`, following tests/test_collectors.py's
monkeypatch-the-module style. FakeResolver raises the real dnspython exception classes
(dns.resolver.NXDOMAIN/NoAnswer/etc.), so lookup_records()/mail_posture()'s exception handling is
exercised for real, only the transport is faked.
"""
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import dns.exception
import dns.resolver
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.domains import dns as domain_dns  # noqa: E402


class FakeRdata:
    def __init__(self, text: str):
        self._text = text

    def to_text(self) -> str:
        return self._text


class FakeResolver:
    """records maps (qname, rdtype) -> list[str] (answer texts), [] (NoAnswer), or an Exception
    instance to raise. A qname/rdtype pair absent from records raises NXDOMAIN."""

    def __init__(self, records: dict):
        self.records = records
        self.calls: list[tuple] = []

    def resolve(self, qname: str, rdtype: str, raise_on_no_answer: bool = True):
        self.calls.append((qname, rdtype))
        key = (qname, rdtype)
        if key not in self.records:
            raise dns.resolver.NXDOMAIN()
        value = self.records[key]
        if isinstance(value, Exception):
            raise value
        if not value:
            raise dns.resolver.NoAnswer()
        return [FakeRdata(text) for text in value]


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise domain_dns.requests.HTTPError(f"status {self.status_code}")


@dataclass
class FakeSettings:
    resolver: Optional[str]


# --- lookup_records -----------------------------------------------------------------


def test_lookup_records_returns_values_per_type():
    resolver = FakeResolver({
        ("example.com", "A"): ["93.184.216.34"],
        ("example.com", "MX"): ["10 mail.example.com."],
        ("example.com", "NS"): ["ns1.example.com.", "ns2.example.com."],
    })
    results = domain_dns.lookup_records("example.com", resolver=resolver)

    assert set(results.keys()) == {"A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA"}
    assert results["A"] == domain_dns.RecordResult(ok=True, values=("93.184.216.34",), error=None)
    assert results["MX"].values == ("10 mail.example.com.",)
    assert set(results["NS"].values) == {"ns1.example.com.", "ns2.example.com."}


def test_lookup_records_nxdomain_is_ok_empty_data():
    resolver = FakeResolver({})  # nothing configured -> every type NXDOMAINs
    results = domain_dns.lookup_records("nowhere.invalid", resolver=resolver)

    for rdtype, result in results.items():
        assert result.ok is True, rdtype
        assert result.values == ()
        assert result.error is None


def test_lookup_records_noanswer_is_ok_empty_data():
    resolver = FakeResolver({("example.com", "AAAA"): []})
    results = domain_dns.lookup_records("example.com", resolver=resolver)

    assert results["AAAA"] == domain_dns.RecordResult(ok=True, values=(), error=None)


def test_lookup_records_real_failure_sets_ok_false_with_error_class_name():
    resolver = FakeResolver({("example.com", "A"): dns.exception.Timeout()})
    results = domain_dns.lookup_records("example.com", resolver=resolver)

    assert results["A"].ok is False
    assert results["A"].values == ()
    assert results["A"].error == "Timeout"


def test_lookup_records_queries_every_type_once():
    resolver = FakeResolver({})
    domain_dns.lookup_records("example.com", resolver=resolver)

    queried = {rdtype for _, rdtype in resolver.calls}
    assert queried == {"A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA"}


# --- mail_posture: SPF ---------------------------------------------------------------


def test_mail_posture_spf_present_counts_lookups():
    resolver = FakeResolver({
        ("example.com", "TXT"): ['"v=spf1 a mx include:_spf.google.com include:sendgrid.net -all"'],
        ("example.com", "MX"): ["10 mail.example.com."],
    })
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.spf.present is True
    assert posture.spf.lookup_count == 4  # a, mx, include, include
    assert posture.spf.within_limit is True


def test_mail_posture_spf_absent():
    resolver = FakeResolver({("example.com", "TXT"): ['"just some other txt record"']})
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.spf.present is False
    assert posture.spf.record is None
    assert posture.spf.lookup_count is None


def test_mail_posture_spf_over_limit_flags_not_within_limit():
    many_includes = " ".join(f"include:s{i}.example.net" for i in range(12))
    resolver = FakeResolver({("example.com", "TXT"): [f'"v=spf1 {many_includes} -all"']})
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.spf.lookup_count == 12
    assert posture.spf.within_limit is False


# --- mail_posture: DMARC --------------------------------------------------------------


def test_mail_posture_dmarc_policy_parsed():
    resolver = FakeResolver({
        ("_dmarc.example.com", "TXT"): ['"v=DMARC1; p=reject; rua=mailto:d@example.com"'],
    })
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.dmarc.present is True
    assert posture.dmarc.policy == "reject"


def test_mail_posture_dmarc_absent():
    resolver = FakeResolver({})
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.dmarc.present is False
    assert posture.dmarc.policy is None


# --- mail_posture: DKIM ----------------------------------------------------------------


def test_mail_posture_dkim_checks_common_selectors():
    resolver = FakeResolver({
        ("google._domainkey.example.com", "TXT"): ['"v=DKIM1; k=rsa; p=ABC123"'],
    })
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert set(posture.dkim.keys()) == {"google", "selector1", "selector2", "k1", "default"}
    assert posture.dkim["google"].present is True
    assert posture.dkim["google"].record == "v=DKIM1; k=rsa; p=ABC123"
    assert posture.dkim["selector1"].present is False


# --- mail_posture: MTA-STS -------------------------------------------------------------


def test_mail_posture_mta_sts_fetches_policy(monkeypatch):
    resolver = FakeResolver({
        ("_mta-sts.example.com", "TXT"): ['"v=STSv1; id=20260101000000Z"'],
    })
    monkeypatch.setattr(
        domain_dns.requests, "get",
        lambda url, timeout=None: FakeResponse("version: STSv1\nmode: enforce\nmx: mail.example.com\n"),
    )
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.mta_sts.txt_present is True
    assert posture.mta_sts.policy_fetched is True
    assert posture.mta_sts.policy_mode == "enforce"
    assert posture.mta_sts.error is None


def test_mail_posture_mta_sts_absent_skips_fetch(monkeypatch):
    resolver = FakeResolver({})
    calls = []
    monkeypatch.setattr(domain_dns.requests, "get", lambda url, timeout=None: calls.append(url))
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.mta_sts.txt_present is False
    assert posture.mta_sts.policy_fetched is False
    assert calls == []


def test_mail_posture_mta_sts_fetch_failure_is_typed_error(monkeypatch):
    resolver = FakeResolver({
        ("_mta-sts.example.com", "TXT"): ['"v=STSv1; id=1"'],
    })

    def raise_connection_error(url, timeout=None):
        raise domain_dns.requests.ConnectionError("boom")

    monkeypatch.setattr(domain_dns.requests, "get", raise_connection_error)
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.mta_sts.txt_present is True
    assert posture.mta_sts.policy_fetched is False
    assert posture.mta_sts.error == "ConnectionError"


# --- mail_posture: BIMI ----------------------------------------------------------------


def test_mail_posture_bimi_present():
    resolver = FakeResolver({
        ("default._bimi.example.com", "TXT"): ['"v=BIMI1; l=https://example.com/logo.svg"'],
    })
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.bimi.present is True
    assert posture.bimi.record is not None
    assert "v=BIMI1" in posture.bimi.record


def test_mail_posture_bimi_absent():
    resolver = FakeResolver({})
    posture = domain_dns.mail_posture("example.com", resolver=resolver)

    assert posture.bimi.present is False
    assert posture.bimi.record is None


# --- mail_posture: parked-domain flag ---------------------------------------------------


def test_mail_posture_flags_parked_domain_with_weak_spf_dmarc():
    resolver = FakeResolver({})  # no MX, no SPF, no DMARC
    posture = domain_dns.mail_posture("parked-example.com", resolver=resolver)

    assert posture.flags == ("parked domain without v=spf1 -all / p=reject",)


def test_mail_posture_no_flag_when_parked_domain_locked_down():
    resolver = FakeResolver({
        ("locked.example.com", "TXT"): ['"v=spf1 -all"'],
        ("_dmarc.locked.example.com", "TXT"): ['"v=DMARC1; p=reject"'],
    })
    posture = domain_dns.mail_posture("locked.example.com", resolver=resolver)

    assert posture.flags == ()


def test_mail_posture_no_flag_when_domain_receives_mail():
    resolver = FakeResolver({
        ("mailed.example.com", "MX"): ["10 mail.example.com."],
    })
    posture = domain_dns.mail_posture("mailed.example.com", resolver=resolver)

    assert posture.flags == ()


# --- _make_resolver: DOMAINS_RESOLVER wiring ---------------------------------------------


def test_make_resolver_uses_domains_resolver_setting(monkeypatch):
    monkeypatch.setattr(domain_dns, "domain_settings", lambda: FakeSettings(resolver="10.0.0.1, 10.0.0.2"))
    resolver = domain_dns._make_resolver()

    assert resolver.nameservers == ["10.0.0.1", "10.0.0.2"]


def test_make_resolver_defaults_when_unset(monkeypatch):
    monkeypatch.setattr(domain_dns, "domain_settings", lambda: FakeSettings(resolver=None))
    resolver = domain_dns._make_resolver()

    assert isinstance(resolver, dns.resolver.Resolver)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
