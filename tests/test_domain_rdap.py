"""
Unit tests for pipeline.domains.rdap (lookup_rdap).

No network: lookup_rdap()'s bootstrap/is_bootstrapped/domain_lookup are keyword-injectable, so
every test supplies a fake in place of whoisit's real, network-hitting functions. Fakes raise the
real whoisit exception classes (UnsupportedError, ResourceDoesNotExist, ...), so lookup_rdap()'s
exception handling is exercised for real, only the transport is faked.
"""
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest
from whoisit.errors import BootstrapError, QueryError, ResourceDoesNotExist, UnsupportedError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.domains import rdap  # noqa: E402


class FakeBootstrap:
    """Tracks bootstrap() calls; is_bootstrapped() flips True only after bootstrap() runs, so
    lookup_rdap()'s check-lock-check gate is exercised for real."""

    def __init__(self):
        self.calls = 0
        self._bootstrapped = False

    def bootstrap(self, **kwargs):
        self.calls += 1
        self._bootstrapped = True

    def is_bootstrapped(self):
        return self._bootstrapped


def _lookup(name, domain_lookup, bootstrap=None):
    boot = bootstrap or FakeBootstrap()
    return rdap.lookup_rdap(
        name,
        domain_lookup=domain_lookup,
        bootstrap=boot.bootstrap,
        is_bootstrapped=boot.is_bootstrapped,
    )


PARSED_OK = {
    "name": "EXAMPLE.COM",
    "nameservers": ["ns1.example.com", "ns2.example.com"],
    "status": ["client transfer prohibited", "active"],
    "registration_date": datetime(2010, 1, 1),
    "expiration_date": datetime(2027, 1, 1),
    "entities": {
        "registrar": [{"name": "Example Registrar, LLC", "handle": "123-IANA"}],
    },
}


# --- ok ----------------------------------------------------------------------------


def test_lookup_rdap_ok_result():
    result = _lookup("example.com", lambda name: dict(PARSED_OK))

    assert result.status == "ok"
    assert result.registrar == "Example Registrar, LLC"
    assert result.created == datetime(2010, 1, 1)
    assert result.expires == datetime(2027, 1, 1)
    assert result.nameservers == ("ns1.example.com", "ns2.example.com")
    assert result.statuses == ("clientTransferProhibited", "active")
    assert result.error is None


def test_lookup_rdap_locked_true_when_transfer_prohibited_present():
    result = _lookup("example.com", lambda name: dict(PARSED_OK))

    assert result.locked is True


def test_lookup_rdap_locked_false_without_transfer_prohibition():
    parsed = dict(PARSED_OK, status=["server delete prohibited", "active"])
    result = _lookup("example.com", lambda name: parsed)

    assert result.locked is False
    assert result.statuses == ("serverDeleteProhibited", "active")


def test_lookup_rdap_registrar_falls_back_to_handle_without_name():
    parsed = dict(PARSED_OK, entities={"registrar": [{"handle": "123-IANA"}]})
    result = _lookup("example.com", lambda name: parsed)

    assert result.registrar == "123-IANA"


def test_lookup_rdap_registrar_none_without_registrar_entity():
    parsed = dict(PARSED_OK, entities={})
    result = _lookup("example.com", lambda name: parsed)

    assert result.registrar is None


# --- unsupported / not_found / error ------------------------------------------------


def test_lookup_rdap_unsupported_tld():
    def raise_unsupported(name):
        raise UnsupportedError(f'TLD "de" has no known RDAP endpoint')

    result = _lookup("example.de", raise_unsupported)

    assert result.status == "unsupported"
    assert result.error is None
    assert result.nameservers == ()
    assert result.statuses == ()
    assert result.locked is False


def test_lookup_rdap_404_is_not_found():
    def raise_404(name):
        raise ResourceDoesNotExist("not found", status_code=404, response="")

    result = _lookup("unregistered-example.com", raise_404)

    assert result.status == "not_found"
    assert result.error is None


def test_lookup_rdap_other_query_error_is_error_with_class_name_only():
    def raise_500(name):
        raise QueryError("boom with secret detail", status_code=500, response="leaked-token=abc")

    result = _lookup("example.com", raise_500)

    assert result.status == "error"
    assert result.error == "QueryError"
    assert "leaked-token" not in (result.error or "")


def test_lookup_rdap_unexpected_exception_is_error_with_class_name_only():
    def raise_unexpected(name):
        raise ConnectionError("network is down")

    result = _lookup("example.com", raise_unexpected)

    assert result.status == "error"
    assert result.error == "ConnectionError"


def test_lookup_rdap_bootstrap_failure_is_error():
    boot = FakeBootstrap()

    def failing_bootstrap(**kwargs):
        raise BootstrapError("failed to download bootstrap data")

    boot.bootstrap = failing_bootstrap
    result = _lookup("example.com", lambda name: dict(PARSED_OK), bootstrap=boot)

    assert result.status == "error"
    assert result.error == "BootstrapError"


# --- bootstrap once per process, thread-safe ----------------------------------------


def test_lookup_rdap_bootstraps_only_once_across_calls():
    boot = FakeBootstrap()
    _lookup("example.com", lambda name: dict(PARSED_OK), bootstrap=boot)
    _lookup("example.org", lambda name: dict(PARSED_OK), bootstrap=boot)

    assert boot.calls == 1


def test_ensure_bootstrapped_is_thread_safe():
    boot = FakeBootstrap()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        rdap._ensure_bootstrapped(boot.bootstrap, boot.is_bootstrapped)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert boot.calls == 1


# --- status normalization helpers ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("client transfer prohibited", "clientTransferProhibited"),
        ("server delete prohibited", "serverDeleteProhibited"),
        ("active", "active"),
        ("clientHold", "clientHold"),
        ("pending update", "pendingUpdate"),
    ],
)
def test_camel_case_status(raw, expected):
    assert rdap._camel_case_status(raw) == expected


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
