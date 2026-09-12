"""End to end: boot the two-headed mock, run the pipeline, check that the
defects planted in the rewrite are the ones the report names.

This is the test that would have caught every regression in the tool itself
during development, and it is the reason the demo in the README is safe to
quote: the numbers it prints are asserted here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from parity_gate.cli import EXIT_GATE_FAILED, EXIT_OK, EXIT_REFUSED, main
from parity_gate.evidence import FAIL, PASS, WARN, verify
from parity_gate.mock import MockServer, start
from parity_gate.runner import Runner
from parity_gate.suite import load

SUITE = """
name = "integration"
[targets.baseline]
base_url = "{base}/legacy"
[targets.candidate]
base_url = "{base}/next"
[policy]
allowed_hosts = ["127.0.0.1"]
allow_private_networks = true
max_retries = 0
repeats = 3
timeout_seconds = 5
mask_paths = ["$.meta.requestId", "$.meta.generatedAt"]
[policy.array_keys]
"$.products" = "id"

[[cases]]
id = "CAT-001"
requirement = "REQ-CAT-01"
title = "Product listing"
risk = "critical"
method = "GET"
path = "/products?limit=4"
expect_status = 200
required_fields = ["$.products", "$.total"]

[[cases]]
id = "CAT-002"
requirement = "REQ-CAT-02"
title = "Product detail, not migrated"
method = "GET"
path = "/products/1"
expect_status = 200

[[cases]]
id = "CAT-003"
requirement = "REQ-CAT-03"
title = "Unknown id still 404s"
method = "GET"
path = "/products/999"
expect_status = 404

[[cases]]
id = "SEC-001"
requirement = "REQ-SEC-01"
title = "No internal cost on the public catalogue"
risk = "critical"
method = "GET"
path = "/products?limit=2"
forbidden_fields = ["$.products[].internalCost"]

[[cases]]
id = "OPS-001"
requirement = "REQ-OPS-01"
title = "Health"
method = "GET"
path = "/health"

[[cases]]
id = "OPS-002"
requirement = "REQ-OPS-02"
title = "Sync status"
method = "GET"
path = "/inventory/sync-status"
"""

pytestmark = pytest.mark.integration


@pytest.fixture
def mock() -> Iterator[MockServer]:
    server = start(0)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def suite_file(tmp_path: Path, mock: MockServer) -> Path:
    target = tmp_path / "integration.toml"
    target.write_text(SUITE.format(base=mock.base_url), encoding="utf-8")
    return target


def records(suite_file: Path) -> dict[str, object]:
    run = Runner(load(suite_file)).execute()
    return {record.case["id"]: record for record in run.records}


def test_the_run_reaches_the_expected_verdict_per_case(suite_file: Path) -> None:
    found = records(suite_file)
    assert found["CAT-001"].verdict == FAIL
    assert found["CAT-002"].verdict == PASS  # untouched endpoint stays green
    assert found["CAT-003"].verdict == FAIL
    assert found["SEC-001"].verdict == FAIL
    assert found["OPS-001"].verdict == WARN  # noisy, not broken
    assert found["OPS-002"].verdict == WARN  # unstable, so not compared


def test_every_planted_contract_defect_is_named(suite_file: Path) -> None:
    drifts = {d["path"]: d for d in records(suite_file)["CAT-001"].drifts}
    assert drifts["$.products[].price"]["kind"] == "TYPE_CHANGED"
    assert drifts["$.products[].price"]["severity"] == "breaking"
    assert drifts["$.products[].stock"]["kind"] == "FIELD_REMOVED"
    assert drifts["$.products[].discount"]["kind"] == "NULLABLE_ADDED"
    assert drifts["$.products[].warehouseId"]["severity"] == "additive"


def test_the_pagination_bug_survives_the_noise_filters(suite_file: Path) -> None:
    differences = {d["path"]: d for d in records(suite_file)["CAT-001"].differences}
    # 4 items returned, total says 3.
    assert differences["$.total"]["baseline"] == 4
    assert differences["$.total"]["candidate"] == 3
    # Reordering is one finding, not sixteen.
    assert differences["$.products"]["kind"] == "ORDER_ONLY"
    assert len(differences) == 2


def test_the_tolerant_404_is_reported_as_a_failed_assertion(suite_file: Path) -> None:
    record = records(suite_file)["CAT-003"]
    failed = [c for c in record.checks if not c["passed"]]
    assert [c["name"] for c in failed] == ["status"]
    assert "got 200" in failed[0]["detail"]


def test_the_data_exposure_is_caught_by_its_own_requirement(suite_file: Path) -> None:
    record = records(suite_file)["SEC-001"]
    failed = [c for c in record.checks if not c["passed"]]
    assert any("internalCost" in c["detail"] for c in failed)
    assert record.case["requirement"] == "REQ-SEC-01"


def test_the_unstable_endpoint_is_triaged_instead_of_diffed(suite_file: Path) -> None:
    record = records(suite_file)["OPS-002"]
    assert record.stability["candidate"]["verdict"] == "FLAKY_STATUS"
    assert record.drifts == [] and record.differences == []
    assert "Stabilise the endpoint" in (record.error or "")


def test_the_noisy_endpoint_proposes_its_own_mask(suite_file: Path) -> None:
    record = records(suite_file)["OPS-001"]
    assert record.stability["candidate"]["verdict"] == "VOLATILE_BODY"
    assert record.stability["candidate"]["volatile_paths"] == ["$.uptimeSeconds"]


def test_no_credential_reaches_the_evidence(suite_file: Path, monkeypatch) -> None:
    body = suite_file.read_text(encoding="utf-8").replace(
        "[policy]", 'auth = "env:PARITY_IT_TOKEN"\n\n[policy]', 1
    )
    suite_file.write_text(body, encoding="utf-8")
    monkeypatch.setenv("PARITY_IT_TOKEN", "ghp_16CharactersMinimumAAAA")

    run = Runner(load(suite_file)).execute()
    serialised = json.dumps(run.to_dict())
    assert "ghp_16CharactersMinimumAAAA" not in serialised
    assert "[REDACTED]" in serialised


def test_the_cli_writes_a_bundle_that_verifies(suite_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "evidence"
    code = main(["run", "--suite", str(suite_file), "--evidence", str(out), "--quiet"])
    assert code == EXIT_GATE_FAILED

    bundles = list(out.iterdir())
    assert len(bundles) == 1
    bundle = bundles[0]
    assert {p.name for p in bundle.iterdir()} == {
        "run.json",
        "report.md",
        "report.html",
        "manifest.json",
    }
    assert verify(bundle) == []
    assert main(["verify", str(bundle)]) == EXIT_OK


def test_the_cli_refuses_a_host_the_policy_does_not_cover(
    suite_file: Path, tmp_path: Path
) -> None:
    body = suite_file.read_text(encoding="utf-8").replace(
        'allowed_hosts = ["127.0.0.1"]', 'allowed_hosts = ["api.example.com"]'
    )
    suite_file.write_text(body, encoding="utf-8")
    assert main(["run", "--suite", str(suite_file), "--evidence", str(tmp_path), "--quiet"]) == (
        EXIT_REFUSED
    )


def test_a_run_against_itself_is_clean(mock: MockServer, tmp_path: Path) -> None:
    """Baseline and candidate pointed at the same variant must agree.

    This is the control experiment: if comparing something to itself produced
    findings, none of the findings above would mean anything.
    """
    body = SUITE.format(base=mock.base_url).replace("/next", "/legacy")
    target = tmp_path / "self.toml"
    target.write_text(body, encoding="utf-8")

    found = {r.case["id"]: r for r in Runner(load(target)).execute().records}
    assert found["CAT-001"].drifts == []
    assert found["CAT-001"].differences == []
    assert found["CAT-002"].verdict == PASS
    assert found["CAT-003"].verdict == PASS
