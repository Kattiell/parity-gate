"""The evidence bundle is the deliverable. These tests cover the promise it
makes: that an altered record is detectable."""

from __future__ import annotations

import json
from pathlib import Path

from parity_gate.evidence import (
    FAIL,
    GENESIS,
    PASS,
    WARN,
    Record,
    Run,
    verify,
    write_manifest,
    write_run,
)


def make_run() -> Run:
    run = Run(
        run_id="test-run",
        suite_name="s",
        suite_path="suites/s.toml",
        suite_sha256="abc",
        baseline_url="http://127.0.0.1/legacy",
        candidate_url="http://127.0.0.1/next",
        policy={},
    )
    run.add(Record(case={"id": "A-1", "requirement": "REQ-1", "risk": "high"}, verdict=PASS))
    run.add(
        Record(
            case={"id": "A-2", "requirement": "REQ-1", "risk": "critical"},
            verdict=FAIL,
            drifts=[{"path": "$.p", "kind": "TYPE_CHANGED", "severity": "breaking", "detail": ""}],
        )
    )
    run.add(Record(case={"id": "A-3", "requirement": None, "risk": "low"}, verdict=WARN))
    run.finished_at = "2026-01-01T00:00:00Z"
    return run


def test_the_first_record_links_to_the_genesis_hash() -> None:
    run = make_run()
    assert run.records[0].previous_hash == GENESIS
    assert run.records[1].previous_hash == run.records[0].record_hash


def test_the_run_verdict_is_the_worst_case_verdict() -> None:
    assert make_run().verdict == FAIL


def test_the_traceability_matrix_groups_cases_by_requirement() -> None:
    matrix = make_run().traceability()
    assert [entry["case"] for entry in matrix["REQ-1"]] == ["A-1", "A-2"]
    assert "(unmapped)" in matrix


def test_an_intact_bundle_verifies(tmp_path: Path) -> None:
    run = make_run()
    files = write_run(run, tmp_path)
    write_manifest(tmp_path, files, run.chain_head, run.run_id)
    assert verify(tmp_path) == []


def test_editing_a_verdict_after_the_fact_is_detected(tmp_path: Path) -> None:
    run = make_run()
    files = write_run(run, tmp_path)
    write_manifest(tmp_path, files, run.chain_head, run.run_id)

    data = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    data["records"][1]["verdict"] = PASS  # the report said FAIL; someone "fixed" it
    (tmp_path / "run.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    problems = verify(tmp_path)
    assert any("A-2" in problem and "altered" in problem for problem in problems)
    assert any("sha256 mismatch" in problem for problem in problems)


def test_a_missing_file_named_in_the_manifest_is_reported(tmp_path: Path) -> None:
    run = make_run()
    files = write_run(run, tmp_path)
    files["report.md"] = "0" * 64
    write_manifest(tmp_path, files, run.chain_head, run.run_id)
    assert any("report.md" in problem for problem in verify(tmp_path))


def test_verifying_an_empty_directory_says_what_is_missing(tmp_path: Path) -> None:
    assert verify(tmp_path) == [f"{tmp_path / 'manifest.json'} is missing"]


def test_summary_counts_breaking_drift_across_records() -> None:
    summary = make_run().to_dict()["summary"]
    assert summary["breaking_drifts"] == 1
    assert summary["by_verdict"][FAIL] == 1
    assert summary["cases"] == 3
