"""Suite validation: a harness that accepts a broken file and fails later is
worse than one that refuses it at the door."""

from __future__ import annotations

from pathlib import Path

import pytest

from parity_gate.suite import SuiteError, load

MINIMAL = """
name = "s"
[targets.baseline]
base_url = "http://127.0.0.1:9/legacy"
[targets.candidate]
base_url = "http://127.0.0.1:9/next"
[policy]
allowed_hosts = ["127.0.0.1"]
[[cases]]
id = "A-1"
title = "t"
method = "GET"
path = "/x"
"""


def write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "suite.toml"
    target.write_text(body, encoding="utf-8")
    return target


def test_a_minimal_suite_loads_with_sensible_defaults(tmp_path: Path) -> None:
    suite = load(write(tmp_path, MINIMAL))
    assert suite.name == "s"
    assert suite.repeats == 3
    assert suite.cases[0].risk == "medium"
    assert suite.cases[0].mutating is False
    assert suite.policy.allow_mutations is False


def test_a_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="not found"):
        load(tmp_path / "nope.toml")


def test_both_targets_are_required(tmp_path: Path) -> None:
    body = MINIMAL.replace('[targets.candidate]\nbase_url = "http://127.0.0.1:9/next"', "")
    with pytest.raises(SuiteError, match=r"targets\.baseline"):
        load(write(tmp_path, body))


def test_an_empty_host_allowlist_is_rejected(tmp_path: Path) -> None:
    body = MINIMAL.replace('allowed_hosts = ["127.0.0.1"]', "allowed_hosts = []")
    with pytest.raises(SuiteError, match="allowed_hosts is empty"):
        load(write(tmp_path, body))


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="duplicate case id"):
        load(write(tmp_path, MINIMAL + '\n[[cases]]\nid = "A-1"\nmethod = "GET"\npath = "/y"\n'))


def test_an_unknown_method_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="method"):
        load(write(tmp_path, MINIMAL.replace('method = "GET"', 'method = "FETCH"')))


def test_a_relative_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="must start with"):
        load(write(tmp_path, MINIMAL.replace('path = "/x"', 'path = "x"')))


def test_an_unknown_risk_level_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="risk"):
        load(write(tmp_path, MINIMAL + 'risk = "apocalyptic"\n'))


def test_a_literal_credential_in_the_suite_blocks_the_run(tmp_path: Path) -> None:
    leaked = 'Authorization = "Bearer ghp_16CharsMinimumAA"'
    body = MINIMAL + f"\n[targets.candidate.headers]\n{leaked}\n"
    with pytest.raises(SuiteError, match="live credential"):
        load(write(tmp_path, body))


def test_auth_must_reference_an_environment_variable(tmp_path: Path) -> None:
    body = MINIMAL.replace(
        '[targets.candidate]\nbase_url = "http://127.0.0.1:9/next"',
        '[targets.candidate]\nbase_url = "http://127.0.0.1:9/next"\nauth = "plain-value"',
    )
    with pytest.raises(SuiteError, match="env:VARIABLE_NAME"):
        load(write(tmp_path, body))


def test_a_missing_credential_names_the_variable_not_its_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = MINIMAL.replace(
        '[targets.candidate]\nbase_url = "http://127.0.0.1:9/next"',
        '[targets.candidate]\nbase_url = "http://127.0.0.1:9/next"\nauth = "env:PARITY_TEST_TOKEN"',
    )
    monkeypatch.delenv("PARITY_TEST_TOKEN", raising=False)
    suite = load(write(tmp_path, body))
    with pytest.raises(SuiteError, match="PARITY_TEST_TOKEN"):
        suite.candidate.resolved_headers()


def test_a_present_credential_is_sent_as_a_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = MINIMAL.replace(
        '[targets.candidate]\nbase_url = "http://127.0.0.1:9/next"',
        '[targets.candidate]\nbase_url = "http://127.0.0.1:9/next"\nauth = "env:PARITY_TEST_TOKEN"',
    )
    monkeypatch.setenv("PARITY_TEST_TOKEN", "s3cret")
    suite = load(write(tmp_path, body))
    assert suite.candidate.resolved_headers()["Authorization"] == "Bearer s3cret"


def test_case_masks_extend_rather_than_replace_suite_masks(tmp_path: Path) -> None:
    body = MINIMAL.replace(
        'allowed_hosts = ["127.0.0.1"]',
        'allowed_hosts = ["127.0.0.1"]\nmask_paths = ["$.a"]',
    ) + 'mask_paths = ["$.b"]\n'
    suite = load(write(tmp_path, body))
    assert suite.diff_options(suite.cases[0]).mask_paths == ["$.a", "$.b"]


def test_the_bundled_demo_suite_is_valid() -> None:
    suite = load(Path(__file__).resolve().parent.parent / "suites" / "demo-catalog.toml")
    assert len(suite.cases) >= 6
    assert suite.array_keys == {"$.products": "id"}
    assert suite.policy.allow_mutations is False
