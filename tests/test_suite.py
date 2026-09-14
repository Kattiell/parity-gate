"""Suite validation: a harness that accepts a broken file and fails later is
worse than one that refuses it at the door.

parity-gate:allow-secrets-file - every credential-shaped string below is a
fixture or a pattern definition, never a live value.
"""

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
    body = (
        MINIMAL.replace(
            'allowed_hosts = ["127.0.0.1"]',
            'allowed_hosts = ["127.0.0.1"]\nmask_paths = ["$.a"]',
        )
        + 'mask_paths = ["$.b"]\n'
    )
    suite = load(write(tmp_path, body))
    assert suite.diff_options(suite.cases[0]).mask_paths == ["$.a", "$.b"]


def test_the_bundled_demo_suite_is_valid() -> None:
    from parity_gate import demo

    suite = load(demo.suite_path("differential"))
    assert len(suite.cases) >= 6
    assert suite.array_keys == {"$.products": "id"}
    assert suite.policy.allow_mutations is False


SNAPSHOT_BASELINE = MINIMAL.replace(
    '[targets.baseline]\nbase_url = "http://127.0.0.1:9/legacy"',
    '[targets.baseline]\nsnapshot = "contracts/api.json"',
)


def test_the_baseline_may_be_a_recorded_contract(tmp_path: Path) -> None:
    suite = load(write(tmp_path, SNAPSHOT_BASELINE))
    assert suite.baseline.is_recorded
    assert suite.baseline.label == "contracts/api.json"
    assert suite.candidate.is_recorded is False


def test_a_contract_path_resolves_against_the_suite_file_not_the_cwd(tmp_path: Path) -> None:
    # So a suite directory can be checked out anywhere and still find its own
    # contracts, whatever directory the command happens to be run from.
    source = write(tmp_path, SNAPSHOT_BASELINE)
    assert load(source).contract_path == tmp_path / "contracts" / "api.json"


def test_a_live_baseline_has_no_contract_path(tmp_path: Path) -> None:
    assert load(write(tmp_path, MINIMAL)).contract_path is None


def test_a_target_cannot_be_both_live_and_recorded(tmp_path: Path) -> None:
    body = MINIMAL.replace(
        '[targets.baseline]\nbase_url = "http://127.0.0.1:9/legacy"',
        '[targets.baseline]\nbase_url = "http://x"\nsnapshot = "contracts/api.json"',
    )
    with pytest.raises(SuiteError, match="not both"):
        load(write(tmp_path, body))


def test_a_target_with_neither_says_what_is_missing(tmp_path: Path) -> None:
    body = MINIMAL.replace(
        '[targets.baseline]\nbase_url = "http://127.0.0.1:9/legacy"',
        "[targets.baseline]\n",
    )
    with pytest.raises(SuiteError, match=r"base_url .* or snapshot"):
        load(write(tmp_path, body))


def test_the_candidate_has_to_be_a_running_service(tmp_path: Path) -> None:
    body = MINIMAL.replace(
        '[targets.candidate]\nbase_url = "http://127.0.0.1:9/next"',
        '[targets.candidate]\nsnapshot = "contracts/api.json"',
    )
    with pytest.raises(SuiteError, match=r"only targets\.baseline"):
        load(write(tmp_path, body))


def test_the_bundled_contract_guard_suite_is_valid() -> None:
    from parity_gate import demo

    suite = load(demo.suite_path("contract"))
    assert suite.baseline.is_recorded
    assert suite.contract_path is not None
    assert suite.contract_path.is_file(), "the demo contract should ship with the package"


def test_demo_assets_resolve_without_a_clone() -> None:
    """They live in the package precisely so a wheel install works anywhere."""
    from parity_gate import demo

    for mode in ("differential", "contract", "stability"):
        assert demo.suite_path(mode).is_file()


def test_an_unknown_demo_mode_is_refused() -> None:
    from parity_gate import demo

    with pytest.raises(ValueError, match="unknown demo mode"):
        demo.suite_path("nope")


def test_retries_are_off_by_default(tmp_path: Path) -> None:
    """Retrying a 503 would hide the flakiness the tool exists to surface."""
    assert load(write(tmp_path, MINIMAL)).policy.max_retries == 0


def test_workers_default_to_one_and_cannot_be_zero(tmp_path: Path) -> None:
    assert load(write(tmp_path, MINIMAL)).policy.workers == 1
    hosts = 'allowed_hosts = ["127.0.0.1"]'
    body = MINIMAL.replace(hosts, f"{hosts}\nworkers = 0")
    assert load(write(tmp_path, body)).policy.workers == 1


def test_a_filter_matches_by_id_requirement_or_title(tmp_path: Path) -> None:
    body = (
        MINIMAL
        + """
[[cases]]
id = "B-2"
requirement = "REQ-ORDERS"
title = "Orders listing"
method = "GET"
path = "/orders"
"""
    )
    suite = load(write(tmp_path, body))
    assert [c.id for c in suite.select([])] == ["A-1", "B-2"]  # no filter: everything
    assert [c.id for c in suite.select(["B-*"])] == ["B-2"]  # glob on the id
    assert [c.id for c in suite.select(["REQ-ORDERS"])] == ["B-2"]  # the requirement
    assert [c.id for c in suite.select(["orders"])] == ["B-2"]  # substring of the title
    assert suite.select(["nothing-like-this"]) == []


def test_a_case_matching_two_patterns_is_not_run_twice(tmp_path: Path) -> None:
    suite = load(write(tmp_path, MINIMAL))
    assert [c.id for c in suite.select(["A-*", "A-1"])] == ["A-1"]


def test_response_bounds_default_to_a_deadline_and_a_size_cap(tmp_path: Path) -> None:
    suite = load(write(tmp_path, MINIMAL))
    assert suite.policy.timeout_seconds == 10.0
    assert suite.policy.max_response_bytes == 10 * 1024 * 1024


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("timeout_seconds = 0", "timeout_seconds must be > 0"),
        ("max_response_bytes = 0", "max_response_bytes must be >= 1"),
    ],
)
def test_response_bounds_that_would_disable_themselves_are_rejected(
    tmp_path: Path, setting: str, message: str
) -> None:
    body = MINIMAL.replace("[policy]", f"[policy]\n{setting}", 1)
    with pytest.raises(SuiteError, match=message):
        load(write(tmp_path, body))
