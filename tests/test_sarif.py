"""SARIF output: valid enough for code scanning, pointed at the right line,
and stable enough across runs that an alert is tracked instead of reopened."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from parity_gate import rules, sarif
from parity_gate.cli import EXIT_GATE_FAILED, main
from parity_gate.contracts import save
from parity_gate.mock import MockServer, start
from parity_gate.runner import Runner
from parity_gate.suite import load

pytestmark = pytest.mark.integration

SUITE = """
name = "sarif"
[targets.baseline]
{baseline}
[targets.candidate]
base_url = "{base}/next"
[policy]
allowed_hosts = ["127.0.0.1"]
allow_private_networks = true
repeats = 3
timeout_seconds = 5
mask_paths = ["$.meta.requestId", "$.meta.generatedAt"]
[policy.array_keys]
"$.products" = "id"

[[cases]]
id = "CAT-001"
requirement = "REQ-CAT-01"
title = "Product listing"
path = "/products?limit=4"

[[cases]]
id = "SEC-001"
title = "No internal cost"
path = "/products?limit=2"
forbidden_fields = ["$.products[].internalCost"]
"""

LEVELS = {"error", "warning", "note", "none"}


@pytest.fixture
def mock() -> Iterator[MockServer]:
    server = start(0)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _suite(tmp_path: Path, mock: MockServer, *, recorded: bool) -> Path:
    baseline = 'snapshot = "recorded.json"' if recorded else f'base_url = "{mock.base_url}/legacy"'
    target = tmp_path / "suite.toml"
    target.write_text(SUITE.format(base=mock.base_url, baseline=baseline), encoding="utf-8")
    if recorded:
        runner = Runner(load(target))
        runner.suite.candidate.base_url = f"{mock.base_url}/legacy"

        save(runner.record_contract(), tmp_path / "recorded.json")
    return target


def _log(tmp_path: Path, suite_file: Path) -> dict:  # type: ignore[type-arg]
    run = Runner(load(suite_file)).execute()
    return sarif.render(run, root=tmp_path)


def test_the_log_is_structurally_valid_sarif(tmp_path: Path, mock: MockServer) -> None:
    log = _log(tmp_path, _suite(tmp_path, mock, recorded=False))

    assert log["version"] == "2.1.0"
    assert log["$schema"].endswith("sarif-2.1.0.json")
    (run,) = log["runs"]
    declared = {rule["id"] for rule in run["tool"]["driver"]["rules"]}
    assert declared == {rule.id for rule in rules.RULES}
    assert run["results"], "the demo defects must produce results"
    for result in run["results"]:
        assert result["ruleId"] in declared
        assert result["level"] in LEVELS
        assert result["message"]["text"]
        physical = result["locations"][0]["physicalLocation"]
        assert physical["artifactLocation"]["uri"]
        assert physical["region"]["startLine"] >= 1
        assert result["partialFingerprints"]["parityGate/v1"]


def test_a_contract_drift_points_at_the_fields_own_line_in_the_contract(
    tmp_path: Path, mock: MockServer
) -> None:
    log = _log(tmp_path, _suite(tmp_path, mock, recorded=True))
    lines = (tmp_path / "recorded.json").read_text(encoding="utf-8").splitlines()

    price = next(
        r
        for r in log["runs"][0]["results"]
        if r["ruleId"] == "PG1004" and r["properties"]["case"] == "CAT-001"
    )
    physical = price["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"]["uri"] == "recorded.json"
    assert '"path": "$.products[].price"' in lines[physical["region"]["startLine"] - 1]


def test_a_failed_assertion_points_at_the_case_in_the_suite(
    tmp_path: Path, mock: MockServer
) -> None:
    suite_file = _suite(tmp_path, mock, recorded=False)
    log = _log(tmp_path, suite_file)
    lines = suite_file.read_text(encoding="utf-8").splitlines()

    exposure = next(r for r in log["runs"][0]["results"] if r["ruleId"] == "PG3006")
    physical = exposure["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"]["uri"] == "suite.toml"
    assert lines[physical["region"]["startLine"] - 1] == 'id = "SEC-001"'


def test_the_same_finding_keeps_its_fingerprint_across_runs(
    tmp_path: Path, mock: MockServer
) -> None:
    suite_file = _suite(tmp_path, mock, recorded=False)

    def prints() -> set[tuple[str, str]]:
        log = _log(tmp_path, suite_file)
        return {
            (r["ruleId"], r["partialFingerprints"]["parityGate/v1"])
            for r in log["runs"][0]["results"]
        }

    assert prints() == prints()


def test_the_bundle_carries_the_log_in_its_manifest_and_a_fixed_copy_on_request(
    tmp_path: Path, mock: MockServer
) -> None:
    suite_file = _suite(tmp_path, mock, recorded=False)
    evidence_dir = tmp_path / "evidence"
    fixed = tmp_path / "upload" / "parity-gate.sarif"

    code = main(
        [
            "run",
            "--suite",
            str(suite_file),
            "--evidence",
            str(evidence_dir),
            "--sarif",
            str(fixed),
            "--quiet",
        ]
    )

    assert code == EXIT_GATE_FAILED
    (bundle,) = list(evidence_dir.iterdir())
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert "report.sarif" in manifest["files"]
    assert fixed.read_text(encoding="utf-8") == (bundle / "report.sarif").read_text(
        encoding="utf-8"
    )
