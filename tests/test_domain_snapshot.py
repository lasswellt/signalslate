"""
Unit tests for pipeline.domains.snapshot (inspect, diff).

No network: inspect()'s lookup_records/mail_posture/lookup_rdap are keyword-injectable, so every
inspect() test supplies a fake in place of pipeline.domains.dns/rdap's real, network-hitting
functions -- following tests/test_domain_dns.py and tests/test_domain_rdap.py's injection style
rather than mocking pipeline.domains.dns/rdap. diff() tests build snapshot dicts directly (the
shape inspect() returns) since diff() is pure and takes no lookups at all.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import Domain  # noqa: E402
from pipeline.domains import snapshot  # noqa: E402
from pipeline.domains.dns import (  # noqa: E402
    DkimResult,
    DmarcResult,
    MailPosture,
    MtaStsResult,
    BimiResult,
    RecordResult,
    SpfResult,
)
from pipeline.domains.rdap import RdapResult  # noqa: E402


# --- fakes for inspect() -----------------------------------------------------------

def _ok(values=()):
    return RecordResult(ok=True, values=tuple(values), error=None)


def fake_records(overrides=None):
    base = {
        "A": _ok(["1.2.3.4"]),
        "AAAA": _ok(),
        "CNAME": _ok(),
        "MX": _ok(["10 mail.example.com."]),
        "NS": _ok(["ns1.example.com.", "ns2.example.com."]),
        "TXT": _ok(),
        "SOA": _ok(["ns1.example.com. hostmaster.example.com. 1 2 3 4 5"]),
        "CAA": _ok(),
    }
    if overrides:
        base.update(overrides)

    def _lookup(name):
        return base

    return _lookup


def fake_mail_posture(flags=(), spf_present=True, dmarc_policy="reject"):
    posture = MailPosture(
        spf=SpfResult(present=spf_present, record="v=spf1 -all" if spf_present else None,
                       lookup_count=0 if spf_present else None, within_limit=True if spf_present else None, error=None),
        dmarc=DmarcResult(present=dmarc_policy is not None, record=f"v=DMARC1; p={dmarc_policy}" if dmarc_policy else None,
                           policy=dmarc_policy, error=None),
        dkim={"google": DkimResult(present=False, record=None, error=None)},
        mta_sts=MtaStsResult(txt_present=False, txt_record=None, policy_fetched=False, policy_mode=None, error=None),
        bimi=BimiResult(present=False, record=None, error=None),
        flags=tuple(flags),
    )

    def _lookup(name):
        return posture

    return _lookup


def fake_rdap(status="ok", locked=True, statuses=("clientTransferProhibited",), expires=None):
    result = RdapResult(
        status=status,
        registrar="Example Registrar, LLC" if status == "ok" else None,
        created=datetime(2010, 1, 1) if status == "ok" else None,
        expires=expires if status == "ok" else None,
        nameservers=("ns1.example.com", "ns2.example.com") if status == "ok" else (),
        statuses=tuple(statuses) if status == "ok" else (),
        locked=locked if status == "ok" else False,
        error=None if status in ("ok", "not_found", "unsupported") else "WhoisItError",
    )

    def _lookup(name):
        return result

    return _lookup


def _domain(
    *,
    ownership: str = "owned",
    auto_renew: Optional[bool] = None,
    expires_at: Optional[datetime] = None,
) -> Domain:
    return Domain(name="example.com", ownership=ownership, source="manual", auto_renew=auto_renew, expires_at=expires_at)


# --- inspect() -----------------------------------------------------------------------

def test_inspect_assembles_all_three_parts_and_is_json_serializable():
    result = snapshot.inspect(
        "example.com",
        lookup_records=fake_records(),
        mail_posture=fake_mail_posture(),
        lookup_rdap=fake_rdap(expires=datetime(2027, 1, 1)),
    )

    assert result["dns"]["status"] == "ok"
    assert result["dns"]["records"]["NS"]["values"] == ["ns1.example.com.", "ns2.example.com."]
    assert result["mail"]["status"] == "ok"
    assert result["mail"]["spf"]["present"] is True
    assert result["rdap"]["status"] == "ok"
    assert result["rdap"]["expires"] == "2027-01-01T00:00:00Z"
    assert result["rdap"]["locked"] is True
    assert isinstance(result["data_hash"], str) and len(result["data_hash"]) == 64
    # round-trips through json with no custom encoder
    json.dumps(result)


def test_inspect_hash_is_stable_and_order_independent():
    a = snapshot.inspect("example.com", lookup_records=fake_records(), mail_posture=fake_mail_posture(), lookup_rdap=fake_rdap())
    b = snapshot.inspect("example.com", lookup_records=fake_records(), mail_posture=fake_mail_posture(), lookup_rdap=fake_rdap())
    assert a["data_hash"] == b["data_hash"]


def test_inspect_hash_changes_when_data_changes():
    a = snapshot.inspect("example.com", lookup_records=fake_records(), mail_posture=fake_mail_posture(), lookup_rdap=fake_rdap())
    b = snapshot.inspect(
        "example.com",
        lookup_records=fake_records({"A": _ok(["9.9.9.9"])}),
        mail_posture=fake_mail_posture(),
        lookup_rdap=fake_rdap(),
    )
    assert a["data_hash"] != b["data_hash"]


def test_inspect_dns_part_failure_does_not_fail_whole_snapshot():
    def raising(name):
        raise RuntimeError("boom")

    result = snapshot.inspect(
        "example.com", lookup_records=raising, mail_posture=fake_mail_posture(), lookup_rdap=fake_rdap()
    )
    assert result["dns"]["status"] == "error"
    assert result["dns"]["error"] == "RuntimeError"
    assert result["mail"]["status"] == "ok"
    assert result["rdap"]["status"] == "ok"


def test_inspect_rdap_status_passes_through():
    result = snapshot.inspect(
        "example.com", lookup_records=fake_records(), mail_posture=fake_mail_posture(),
        lookup_rdap=fake_rdap(status="unsupported"),
    )
    assert result["rdap"]["status"] == "unsupported"


# --- diff(): record-set changes --------------------------------------------------------

def _snap(dns_records=None, mail_flags=(), mail_spf_present=True, mail_dmarc_policy="reject",
          rdap_status="ok", rdap_locked=True, rdap_statuses=("clientTransferProhibited",), rdap_expires=None):
    return snapshot.inspect(
        "example.com",
        lookup_records=fake_records(dns_records),
        mail_posture=fake_mail_posture(flags=mail_flags, spf_present=mail_spf_present, dmarc_policy=mail_dmarc_policy),
        lookup_rdap=fake_rdap(status=rdap_status, locked=rdap_locked, statuses=rdap_statuses, expires=rdap_expires),
    )


def test_diff_no_prev_yields_no_record_changes():
    cur = _snap()
    changes = snapshot.diff(None, cur, _domain())
    assert not any(c.kind in ("ns_changed", "mx_changed", "a_changed", "lock_removed", "mail_posture_regressed") for c in changes)


def test_diff_ns_changed():
    prev = _snap()
    cur = _snap(dns_records={"NS": _ok(["ns3.other.com."])})
    changes = snapshot.diff(prev, cur, _domain())
    kinds = [c.kind for c in changes]
    assert "ns_changed" in kinds
    assert "mx_changed" not in kinds
    assert "a_changed" not in kinds


def test_diff_mx_changed():
    prev = _snap()
    cur = _snap(dns_records={"MX": _ok(["20 backup.example.com."])})
    changes = snapshot.diff(prev, cur, _domain())
    assert "mx_changed" in [c.kind for c in changes]


def test_diff_a_changed():
    prev = _snap()
    cur = _snap(dns_records={"A": _ok(["5.6.7.8"])})
    changes = snapshot.diff(prev, cur, _domain())
    assert "a_changed" in [c.kind for c in changes]


def test_diff_no_change_yields_no_record_changes():
    prev = _snap()
    cur = _snap()
    changes = snapshot.diff(prev, cur, _domain())
    assert changes == []


def test_diff_dns_error_section_suppresses_record_diff():
    prev = _snap()
    cur = snapshot.inspect(
        "example.com",
        lookup_records=lambda name: (_ for _ in ()).throw(RuntimeError("boom")),
        mail_posture=fake_mail_posture(),
        lookup_rdap=fake_rdap(),
    )
    changes = snapshot.diff(prev, cur, _domain())
    assert not any(c.kind in ("ns_changed", "mx_changed", "a_changed") for c in changes)


# --- diff(): lock_removed -------------------------------------------------------------

def test_diff_lock_removed():
    prev = _snap(rdap_locked=True, rdap_statuses=("clientTransferProhibited",))
    cur = _snap(rdap_locked=False, rdap_statuses=("active",))
    changes = snapshot.diff(prev, cur, _domain())
    assert "lock_removed" in [c.kind for c in changes]


def test_diff_lock_stays_on_is_not_a_change():
    prev = _snap(rdap_locked=True)
    cur = _snap(rdap_locked=True)
    changes = snapshot.diff(prev, cur, _domain())
    assert "lock_removed" not in [c.kind for c in changes]


# --- diff(): mail_posture_regressed -----------------------------------------------------

def test_diff_mail_posture_regressed_new_flag():
    prev = _snap(mail_flags=())
    cur = _snap(mail_flags=("parked domain without v=spf1 -all / p=reject",))
    changes = snapshot.diff(prev, cur, _domain())
    assert "mail_posture_regressed" in [c.kind for c in changes]


def test_diff_mail_posture_regressed_spf_removed():
    prev = _snap(mail_spf_present=True)
    cur = _snap(mail_spf_present=False)
    changes = snapshot.diff(prev, cur, _domain())
    assert "mail_posture_regressed" in [c.kind for c in changes]


def test_diff_mail_posture_regressed_dmarc_weakened():
    prev = _snap(mail_dmarc_policy="reject")
    cur = _snap(mail_dmarc_policy="none")
    changes = snapshot.diff(prev, cur, _domain())
    assert "mail_posture_regressed" in [c.kind for c in changes]


def test_diff_mail_posture_unchanged_is_not_a_change():
    prev = _snap()
    cur = _snap()
    changes = snapshot.diff(prev, cur, _domain())
    assert "mail_posture_regressed" not in [c.kind for c in changes]


# --- diff(): expiry_soon ----------------------------------------------------------------

def test_diff_expiry_soon_within_30_days_auto_renew_off():
    now = datetime(2026, 1, 1)
    cur = _snap(rdap_expires=now + timedelta(days=20))
    changes = snapshot.diff(None, cur, _domain(auto_renew=False), now=now)
    found = [c for c in changes if c.kind == "expiry_soon"]
    assert len(found) == 1
    assert "20 day" in found[0].summary


def test_diff_expiry_soon_within_7_days_is_urgent_wording():
    now = datetime(2026, 1, 1)
    cur = _snap(rdap_expires=now + timedelta(days=5))
    changes = snapshot.diff(None, cur, _domain(auto_renew=None), now=now)
    found = [c for c in changes if c.kind == "expiry_soon"]
    assert len(found) == 1
    assert "auto-renew is off" in found[0].summary


def test_diff_expiry_soon_absent_when_auto_renew_true():
    now = datetime(2026, 1, 1)
    cur = _snap(rdap_expires=now + timedelta(days=5))
    changes = snapshot.diff(None, cur, _domain(auto_renew=True), now=now)
    assert "expiry_soon" not in [c.kind for c in changes]


def test_diff_expiry_soon_absent_when_far_out():
    now = datetime(2026, 1, 1)
    cur = _snap(rdap_expires=now + timedelta(days=90))
    changes = snapshot.diff(None, cur, _domain(auto_renew=False), now=now)
    assert "expiry_soon" not in [c.kind for c in changes]


def test_diff_expiry_soon_falls_back_to_domain_expires_at_when_rdap_missing():
    now = datetime(2026, 1, 1)
    cur = _snap(rdap_status="not_found")
    changes = snapshot.diff(None, cur, _domain(auto_renew=False, expires_at=now + timedelta(days=3)), now=now)
    found = [c for c in changes if c.kind == "expiry_soon"]
    assert len(found) == 1


# --- diff(): auto_renew_off --------------------------------------------------------------

def test_diff_auto_renew_off():
    cur = _snap()
    changes = snapshot.diff(None, cur, _domain(auto_renew=False), now=datetime(2026, 1, 1))
    assert "auto_renew_off" in [c.kind for c in changes]


def test_diff_auto_renew_unknown_is_not_off():
    cur = _snap()
    changes = snapshot.diff(None, cur, _domain(auto_renew=None), now=datetime(2026, 1, 1))
    assert "auto_renew_off" not in [c.kind for c in changes]


def test_diff_auto_renew_true_is_not_off():
    cur = _snap()
    changes = snapshot.diff(None, cur, _domain(auto_renew=True), now=datetime(2026, 1, 1))
    assert "auto_renew_off" not in [c.kind for c in changes]


# --- diff(): redemption ------------------------------------------------------------------

def test_diff_redemption_on_watched_domain():
    cur = _snap(rdap_statuses=("redemptionPeriod",))
    changes = snapshot.diff(None, cur, _domain(ownership="watched"), now=datetime(2026, 1, 1))
    assert "redemption" in [c.kind for c in changes]


def test_diff_redemption_absent_on_owned_domain():
    cur = _snap(rdap_statuses=("redemptionPeriod",))
    changes = snapshot.diff(None, cur, _domain(ownership="owned"), now=datetime(2026, 1, 1))
    assert "redemption" not in [c.kind for c in changes]


def test_diff_redemption_absent_when_no_redemption_status():
    cur = _snap(rdap_statuses=("active",))
    changes = snapshot.diff(None, cur, _domain(ownership="watched"), now=datetime(2026, 1, 1))
    assert "redemption" not in [c.kind for c in changes]
