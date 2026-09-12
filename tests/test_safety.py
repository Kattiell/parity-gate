"""The guards only matter if they hold, so every refusal has a test and every
allowance is explicit."""

from __future__ import annotations

import pytest

from parity_gate.safety import Policy, SafetyError, check_method, check_url, host_allowed


def policy(**overrides) -> Policy:
    base = {"allowed_hosts": ["api.staging.example.com", "127.0.0.1"]}
    base.update(overrides)
    return Policy(**base)


def test_an_allowed_host_passes() -> None:
    check_url("https://api.staging.example.com/products", policy())


def test_an_unlisted_host_is_refused() -> None:
    with pytest.raises(SafetyError, match="not in allowed_hosts"):
        check_url("https://api.other.example.com/products", policy())


def test_wildcard_entries_cover_subdomains_only_when_written_that_way() -> None:
    assert host_allowed("a.example.com", [".example.com"])
    assert host_allowed("example.com", [".example.com"])
    assert not host_allowed("a.example.com", ["example.com"])
    assert not host_allowed("evil-example.com", [".example.com"])


def test_a_production_looking_host_is_refused_even_when_allowlisted() -> None:
    with pytest.raises(SafetyError, match="production pattern"):
        check_url("https://api.prod.example.com/x", policy(allowed_hosts=["api.prod.example.com"]))


def test_production_can_be_reached_only_by_saying_so() -> None:
    check_url(
        "https://api.prod.example.com/x",
        policy(allowed_hosts=["api.prod.example.com"], allow_production=True),
    )


def test_loopback_needs_an_explicit_opt_in() -> None:
    with pytest.raises(SafetyError, match="private or loopback"):
        check_url("http://127.0.0.1:8799/legacy", policy())
    check_url("http://127.0.0.1:8799/legacy", policy(allow_private_networks=True))


def test_non_http_schemes_are_refused() -> None:
    with pytest.raises(SafetyError, match="scheme"):
        check_url("file:///etc/passwd", policy(allowed_hosts=[""]))


def test_credentials_in_the_url_are_refused() -> None:
    with pytest.raises(SafetyError, match="credentials embedded"):
        check_url("https://user:secret@api.staging.example.com/x", policy())


def test_reads_never_need_permission() -> None:
    for method in ("GET", "HEAD", "OPTIONS"):
        check_method(method, mutating=False, policy=policy())


def test_a_write_must_be_declared_in_the_suite() -> None:
    with pytest.raises(SafetyError, match="not marked"):
        check_method("DELETE", mutating=False, policy=policy(allow_mutations=True))


def test_a_declared_write_still_needs_the_run_to_allow_mutations() -> None:
    with pytest.raises(SafetyError, match="mutations are disabled"):
        check_method("POST", mutating=True, policy=policy())


def test_a_write_runs_only_when_both_switches_are_on() -> None:
    check_method("POST", mutating=True, policy=policy(allow_mutations=True))
