"""Cross-run history: flaky, regressed or recovered, told apart from bundles on
disk, and only from bundles that still verify."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from parity_gate import evidence, history
from parity_gate.cli import EXIT_GATE_FAILED, EXIT_OK, EXIT_USAGE, main
from parity_gate.evidence import ERROR, FAIL, PASS, WARN, Record, Run


def write_bundle(
    root: Path, index: int, verdicts: dict[str, str], revision: str | None = None
) -> Path:
    """A real, verifiable bundle, as a run would have written it."""
    run = Run(
        run_id=f"20260913T{index:06d}Z-{index:06x}",
        suite_name="catalog",
        suite_path="suite.toml",
        suite_sha256="0" * 64,
        baseline_url="http://legacy",
        candidate_url="http://next",
        policy={},
        context={"revision": revision, "ci": None},
        started_at=f"2026-09-13T00:{index // 60:02d}:{index % 60:02d}Z",
    )
    for case_id, verdict in verdicts.items():
        status = 503 if verdict in {FAIL, ERROR} else 200
        run.add(
            Record(
                case={"id": case_id},
                verdict=verdict,
                stability={"candidate": {"verdict": "STABLE", "statuses": [status] * 3}},
            )
        )
    run.finished_at = run.started_at
    bundle = root / run.run_id
    files = evidence.write_run(run, bundle)
    evidence.write_manifest(bundle, files, run.chain_head, run.run_id)
    return bundle


def timeline(root: Path, sequence: list[str], revisions: list[str | None] | None = None) -> None:
    for index, verdict in enumerate(sequence):
        revision = revisions[index] if revisions else None
        write_bundle(root, index, {"CAT-001": verdict}, revision)


def classify(root: Path) -> str:
    (case,) = history.read(root).cases
    return case.classification


def test_a_case_that_fails_and_recovers_on_its_own_is_intermittent(tmp_path: Path) -> None:
    timeline(tmp_path, [PASS, PASS, FAIL, PASS, PASS])
    assert classify(tmp_path) == history.INTERMITTENT


def test_two_outcomes_on_one_revision_is_intermittent_even_with_a_single_flip(
    tmp_path: Path,
) -> None:
    """Pass then fail looks like a regression, unless nothing was deployed in
    between. The revision is what tells those apart."""
    timeline(tmp_path, [PASS, FAIL, FAIL], revisions=["abc123", "abc123", "abc123"])
    (case,) = history.read(tmp_path).cases
    assert case.classification == history.INTERMITTENT
    assert case.disagreeing_revisions == {"abc123": [FAIL, PASS]}


def test_a_change_that_sticks_is_a_regression_not_flakiness(tmp_path: Path) -> None:
    timeline(tmp_path, [PASS, PASS, FAIL, FAIL], revisions=["a1", "a1", "b2", "b2"])
    assert classify(tmp_path) == history.REGRESSED


def test_a_fix_that_sticks_is_a_recovery(tmp_path: Path) -> None:
    timeline(tmp_path, [ERROR, FAIL, PASS, WARN])
    assert classify(tmp_path) == history.RECOVERED


def test_warnings_and_passes_are_the_same_outcome(tmp_path: Path) -> None:
    timeline(tmp_path, [PASS, WARN, PASS, WARN])
    assert classify(tmp_path) == history.STEADY


def test_a_bundle_that_no_longer_verifies_is_left_out_of_the_history(tmp_path: Path) -> None:
    timeline(tmp_path, [PASS, PASS, PASS])
    tampered = write_bundle(tmp_path, 3, {"CAT-001": FAIL})
    run_file = tampered / "run.json"
    run_file.write_text(run_file.read_text(encoding="utf-8").replace("FAIL", "PASS"), "utf-8")

    found = history.read(tmp_path)

    assert found.bundles_read == 3
    assert tampered.name in found.rejected
    assert found.cases[0].classification == history.STEADY


def test_last_limits_the_window_to_the_most_recent_runs(tmp_path: Path) -> None:
    timeline(tmp_path, [FAIL, PASS, PASS, PASS])
    assert history.read(tmp_path).cases[0].classification == history.RECOVERED
    assert history.read(tmp_path, last=3).cases[0].classification == history.STEADY


def test_the_cli_fails_on_intermittent_cases_only_when_asked(tmp_path: Path) -> None:
    timeline(tmp_path, [PASS, FAIL, PASS])
    out = tmp_path.parent / f"{tmp_path.name}-history.json"

    assert main(["history", str(tmp_path)]) == EXIT_OK
    assert main(["history", str(tmp_path), "--fail-on-intermittent", "--json", str(out)]) == (
        EXIT_GATE_FAILED
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["cases"][0]["classification"] == history.INTERMITTENT
    assert data["cases"][0]["flips"] == 2


def test_the_cli_says_so_when_there_is_nothing_to_read(tmp_path: Path) -> None:
    assert main(["history", str(tmp_path)]) == EXIT_USAGE


# -- what a run records so that history can work ----------------------------------


def test_the_revision_comes_from_the_ci_environment_unless_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("PARITY_GATE_REVISION", "GITHUB_SHA", "CI_COMMIT_SHA", "BUILD_SOURCEVERSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_SHA", "f00dbabe")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert evidence.ci_context() == {"revision": "f00dbabe", "ci": "github-actions"}
    assert evidence.ci_context("explicit")["revision"] == "explicit"


def test_repeated_calls_are_spaced_by_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-to-back samples cannot see a slow period; spacing them can."""
    import parity_gate.runner as runner_module
    from parity_gate.mock import start
    from parity_gate.suite import load

    server = start(0)
    suite_file = tmp_path / "spaced.toml"
    suite_file.write_text(
        f"""
name = "spaced"
[targets.baseline]
base_url = "{server.base_url}/legacy"
[targets.candidate]
base_url = "{server.base_url}/legacy"
[policy]
allowed_hosts = ["127.0.0.1"]
allow_private_networks = true
repeats = 4
repeat_interval_ms = 250
[[cases]]
id = "CAT-002"
path = "/products/1"
""",
        encoding="utf-8",
    )
    pauses: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", pauses.append)
    try:
        run = runner_module.Runner(load(suite_file)).execute()
    finally:
        server.shutdown()
        server.server_close()

    # Four calls per side, so three pauses per side, never one after the last.
    assert pauses == [0.25] * 6
    assert run.to_dict()["sampling"] == {"repeats": 4, "repeat_interval_ms": 250}
