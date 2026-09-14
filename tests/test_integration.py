"""End to end: boot the two-headed mock, run the pipeline, check that the
defects planted in the rewrite are the ones the report names.

This is the test that would have caught every regression in the tool itself
during development, and it is the reason the demo in the README is safe to
quote: the numbers it prints are asserted here.


parity-gate:allow-secrets-file - every credential-shaped string below is a
fixture or a pattern definition, never a live value.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from parity_gate.cli import EXIT_GATE_FAILED, EXIT_OK, EXIT_REFUSED, main
from parity_gate.contracts import ContractError, save
from parity_gate.evidence import ERROR, FAIL, PASS, WARN, verify
from parity_gate.mock import MockServer, start
from parity_gate.runner import Runner
from parity_gate.safety import SafetyError
from parity_gate.suite import SuiteError, load

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
    failed = {c["name"]: c for c in record.checks if not c["passed"]}
    assert "status" in failed and "got 200" in failed["status"]["detail"]
    # And independently of the written assertion, by comparing the two sides.
    assert "status_parity" in failed


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


def test_a_defect_in_the_tool_fails_its_case_and_spares_the_others(
    suite_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception escaping a worker used to end the run with a traceback and
    no evidence at all, discarding every case that had already finished."""
    import parity_gate.runner as runner_module

    real_diff = runner_module.diff

    def explode(baseline, candidate, options):  # type: ignore[no-untyped-def]
        if isinstance(baseline, dict) and "products" in baseline:
            raise RuntimeError("synthetic defect")
        return real_diff(baseline, candidate, options)

    monkeypatch.setattr(runner_module, "diff", explode)
    run = Runner(load(suite_file)).execute()
    found = {record.case["id"]: record for record in run.records}

    assert len(found) == 6
    assert found["CAT-001"].verdict == ERROR
    assert "RuntimeError" in (found["CAT-001"].error or "")
    assert "defect in the tool" in (found["CAT-001"].error or "")
    assert found["CAT-002"].verdict == PASS
    assert run.verdict == ERROR


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


def test_the_cli_refuses_a_host_the_policy_does_not_cover(suite_file: Path, tmp_path: Path) -> None:
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


# -- single-service mode: gate against a contract recorded earlier -----------

CONTRACT_SUITE = """
name = "contract-guard"
[targets.baseline]
snapshot = "recorded.json"
[targets.candidate]
base_url = "{base}/{variant}"
[policy]
allowed_hosts = ["127.0.0.1"]
allow_private_networks = true
max_retries = 0
repeats = 3
timeout_seconds = 5
mask_paths = ["$.meta.requestId", "$.meta.generatedAt"]

[[cases]]
id = "CAT-001"
requirement = "REQ-CAT-01"
title = "Product listing"
risk = "critical"
method = "GET"
path = "/products?limit=4"

[[cases]]
id = "CAT-003"
requirement = "REQ-CAT-03"
title = "Unknown id"
method = "GET"
path = "/products/999"
"""


def _contract_suite(tmp_path: Path, mock: MockServer, variant: str) -> Path:
    target = tmp_path / f"{variant}.toml"
    target.write_text(CONTRACT_SUITE.format(base=mock.base_url, variant=variant), encoding="utf-8")
    return target


def test_a_recorded_contract_gates_a_single_service(tmp_path: Path, mock: MockServer) -> None:
    """Record the old shape, then check the rewrite against the file.

    This is the mode that does not need two live services, which is every team
    that has one API and consumers who cannot be redeployed in lockstep.
    """
    record_from = _contract_suite(tmp_path, mock, "legacy")
    contract = Runner(load(record_from)).record_contract()
    save(contract, tmp_path / "recorded.json")

    gate = _contract_suite(tmp_path, mock, "next")
    found = {r.case["id"]: r for r in Runner(load(gate)).execute().records}

    drifts = {d["path"]: d for d in found["CAT-001"].drifts}
    assert drifts["$.products[].price"]["kind"] == "TYPE_CHANGED"
    assert drifts["$.products[].stock"]["kind"] == "FIELD_REMOVED"
    assert drifts["$.products[].discount"]["kind"] == "NULLABLE_ADDED"
    assert found["CAT-001"].verdict == FAIL


def test_the_recorded_status_catches_a_404_that_became_a_200(
    tmp_path: Path, mock: MockServer
) -> None:
    """No `expect_status` is written anywhere in CONTRACT_SUITE.

    The status an endpoint answers with is part of what gets recorded, so the
    softened error is caught on a case nobody thought to assert on.
    """
    save(
        Runner(load(_contract_suite(tmp_path, mock, "legacy"))).record_contract(),
        tmp_path / "recorded.json",
    )
    gate = Runner(load(_contract_suite(tmp_path, mock, "next"))).execute()
    record = {r.case["id"]: r for r in gate.records}["CAT-003"]

    failed = [c for c in record.checks if not c["passed"]]
    assert [c["name"] for c in failed] == ["recorded_status"]
    assert "404" in failed[0]["detail"] and "200" in failed[0]["detail"]


def test_the_service_checked_against_its_own_recording_is_clean(
    tmp_path: Path, mock: MockServer
) -> None:
    """The control experiment for contract mode."""
    suite_path = _contract_suite(tmp_path, mock, "legacy")
    save(Runner(load(suite_path)).record_contract(), tmp_path / "recorded.json")

    for record in Runner(load(suite_path)).execute().records:
        assert record.drifts == []
        assert record.verdict == PASS, record.error


def test_a_case_with_no_recorded_entry_is_flagged_not_silently_passed(
    tmp_path: Path, mock: MockServer
) -> None:
    suite_path = _contract_suite(tmp_path, mock, "legacy")
    contract = Runner(load(suite_path)).record_contract()
    del contract.cases["CAT-003"]
    save(contract, tmp_path / "recorded.json")

    record = {r.case["id"]: r for r in Runner(load(suite_path)).execute().records}["CAT-003"]
    assert record.verdict == WARN
    assert "no entry in the recorded contract" in (record.error or "")


def test_a_contract_records_shape_and_status_but_never_values(
    tmp_path: Path, mock: MockServer
) -> None:
    contract = Runner(load(_contract_suite(tmp_path, mock, "legacy"))).record_contract()
    text = save(contract, tmp_path / "recorded.json").read_text(encoding="utf-8")

    assert contract.cases["CAT-003"].statuses == [404]
    assert "Torx screwdriver" not in text  # a value, deliberately not recorded
    assert "$.products[].price" in text  # the shape, deliberately recorded


def test_running_without_a_recorded_contract_says_how_to_make_one(
    tmp_path: Path, mock: MockServer
) -> None:
    suite_path = _contract_suite(tmp_path, mock, "next")
    with pytest.raises(ContractError, match="parity-gate record"):
        Runner(load(suite_path)).execute()


# -- stability mode: one service, no comparison at all ----------------------


def test_stability_mode_measures_endpoints_not_expectations(
    suite_file: Path, tmp_path: Path
) -> None:
    run = Runner(load(suite_file)).measure_stability()
    found = {r.case["id"]: r for r in run.records}

    assert found["OPS-002"].verdict == FAIL  # intermittent status
    assert found["OPS-001"].verdict == WARN  # noisy body
    assert found["OPS-001"].stability["candidate"]["volatile_paths"] == ["$.uptimeSeconds"]
    assert found["CAT-001"].verdict == PASS

    # The suite's own assertions are not evaluated here: CAT-003 expects 404 and
    # gets 200, which is a real failure for `run` and none of this mode's business.
    assert found["CAT-003"].verdict == PASS
    assert found["CAT-003"].checks == []


def test_the_stability_cli_writes_a_bundle_and_fails_on_an_unstable_endpoint(
    suite_file: Path, tmp_path: Path
) -> None:
    out = tmp_path / "stability"
    assert main(["stability", "--suite", str(suite_file), "--evidence", str(out), "--quiet"]) == (
        EXIT_GATE_FAILED
    )
    (bundle,) = list(out.iterdir())
    assert verify(bundle) == []


# -- fixes for what the senior review found ---------------------------------

STATUS_SUITE = """
name = "status"
[targets.baseline]
base_url = "{base}/legacy"
[targets.candidate]
base_url = "{base}/next"
[policy]
allowed_hosts = ["127.0.0.1"]
allow_private_networks = true
repeats = 2
[[cases]]
id = "S-1"
title = "unknown id"
method = "GET"
path = "/products/999"
"""


def test_a_status_divergence_fails_without_anyone_having_predicted_it(
    tmp_path: Path, mock: MockServer
) -> None:
    """The false PASS the review found.

    Baseline answers 404, candidate answers 200. No `expect_status` is written
    anywhere in STATUS_SUITE — predicting the change is exactly what the tool
    is supposed to make unnecessary.
    """
    target = tmp_path / "status.toml"
    target.write_text(STATUS_SUITE.format(base=mock.base_url), encoding="utf-8")

    record = Runner(load(target)).execute().records[0]
    failed = {c["name"]: c for c in record.checks if not c["passed"]}
    assert "status_parity" in failed
    assert "404" in failed["status_parity"]["detail"]
    assert "200" in failed["status_parity"]["detail"]
    assert record.verdict == FAIL


def test_parallel_and_sequential_runs_produce_the_same_report(
    suite_file: Path, tmp_path: Path
) -> None:
    """A gate whose output depends on scheduling cannot be diffed between runs."""
    sequential = {
        r.case["id"]: (r.verdict, r.drifts, r.differences)
        for r in Runner(load(suite_file)).execute().records
    }

    body = suite_file.read_text(encoding="utf-8").replace("[policy]", "[policy]\nworkers = 4", 1)
    suite_file.write_text(body, encoding="utf-8")
    parallel_run = Runner(load(suite_file)).execute()
    parallel = {r.case["id"]: (r.verdict, r.drifts, r.differences) for r in parallel_run.records}

    assert [r.case["id"] for r in parallel_run.records] == list(sequential)
    for case_id, expected in sequential.items():
        # OPS-002 is deliberately intermittent; its verdict is the point of the
        # case, not something two runs have to agree on.
        if case_id != "OPS-002":
            assert parallel[case_id] == expected, case_id


def test_a_missing_credential_is_refused_at_the_door(suite_file: Path, monkeypatch) -> None:
    body = suite_file.read_text(encoding="utf-8").replace(
        "[policy]", 'auth = "env:PARITY_MISSING_TOKEN"\n\n[policy]', 1
    )
    suite_file.write_text(body, encoding="utf-8")
    monkeypatch.delenv("PARITY_MISSING_TOKEN", raising=False)

    with pytest.raises(SuiteError, match="PARITY_MISSING_TOKEN"):
        Runner(load(suite_file)).preflight()


def test_contract_mode_says_values_were_not_compared(tmp_path: Path, mock: MockServer) -> None:
    suite_path = _contract_suite(tmp_path, mock, "legacy")
    save(Runner(load(suite_path)).record_contract(), tmp_path / "recorded.json")

    summary = Runner(load(suite_path)).execute().to_dict()["summary"]
    assert summary["values_compared"] is False
    assert summary["differences"] is None


AUTH_SUITE = """
name = "auth"
[targets.baseline]
base_url = "{base}/legacy"
[targets.candidate]
base_url = "{base}/next"
auth = "env:PARITY_E2E_TOKEN"
[policy]
allowed_hosts = ["127.0.0.1"]
allow_private_networks = true
repeats = 1
[[cases]]
id = "AUTH-1"
title = "the credential reaches the server"
method = "GET"
path = "/whoami"
"""


def test_a_credential_reaches_the_server_and_not_the_evidence(
    tmp_path: Path, mock: MockServer, monkeypatch
) -> None:
    """The end-to-end half of the auth path that unit tests cannot cover.

    `resolved_headers` was tested in isolation; nothing asserted the header was
    actually sent. The mock echoes it back, so one run proves both that it
    arrived and that it is gone from the bundle written afterwards.
    """
    token = "ghp_E2ECredential1234567"
    monkeypatch.setenv("PARITY_E2E_TOKEN", token)
    target = tmp_path / "auth.toml"
    target.write_text(AUTH_SUITE.format(base=mock.base_url), encoding="utf-8")

    run = Runner(load(target)).execute()
    record = run.records[0]

    # It arrived: the baseline target carries no credential and echoes null,
    # the candidate echoes a value. If the header had never been sent, both
    # sides would be null and this would pass vacuously.
    assert record.baseline["body"]["authorization"] is None
    assert record.candidate["body"]["authorization"] == "[REDACTED]"

    serialised = json.dumps(run.to_dict())
    assert token not in serialised
    assert "Bearer" not in serialised or "[REDACTED]" in serialised


def test_retention_keeps_the_newest_bundles_and_nothing_else(
    suite_file: Path, tmp_path: Path
) -> None:
    out = tmp_path / "evidence"
    notes = out / "notes-i-keep-here"
    notes.mkdir(parents=True)
    (notes / "x.txt").write_text("mine", encoding="utf-8")

    for _ in range(3):
        main(["run", "--suite", str(suite_file), "--evidence", str(out), "--quiet"])
    assert len([d for d in out.iterdir() if (d / "manifest.json").is_file()]) == 3

    main(["run", "--suite", str(suite_file), "--evidence", str(out), "--keep", "2", "--quiet"])
    bundles = [d for d in out.iterdir() if (d / "manifest.json").is_file()]
    assert len(bundles) == 2
    # A directory that is not a bundle must survive a retention flag.
    assert (notes / "x.txt").read_text(encoding="utf-8") == "mine"
    for bundle in bundles:
        assert verify(bundle) == []


def test_a_filter_that_matches_nothing_names_the_available_cases(
    suite_file: Path, tmp_path: Path, capsys
) -> None:
    code = main(
        ["run", "--suite", str(suite_file), "--evidence", str(tmp_path), "--filter", "NOPE"]
    )
    assert code == 1
    assert "CAT-001" in capsys.readouterr().err


# -- GraphQL: one URL, one method, no status code worth reading --------------

GQL_SUITE = """
name = "graphql"
[targets.baseline]
base_url = "{base}/legacy"
[targets.candidate]
base_url = "{base}/next"
[policy]
allowed_hosts = ["127.0.0.1"]
allow_private_networks = true
repeats = 2
[[cases]]
id = "GQL-001"
requirement = "REQ-GQL-01"
title = "Product query keeps its shape"
risk = "critical"
path = "/graphql"
graphql = "query Products {{ products {{ id title price stock }} }}"
"""


def test_a_graphql_query_runs_without_the_mutation_gate(tmp_path: Path, mock: MockServer) -> None:
    """Every GraphQL operation is a POST. If the write gate went by method,
    this run would be refused and every query would have to lie about being a
    mutation."""
    target = tmp_path / "gql.toml"
    target.write_text(GQL_SUITE.format(base=mock.base_url), encoding="utf-8")

    record = Runner(load(target)).execute().records[0]
    assert record.error is None
    assert record.case["method"] == "POST"
    assert record.case["graphql"] == "query"


def test_errors_in_a_200_are_caught_where_no_status_check_could(
    tmp_path: Path, mock: MockServer
) -> None:
    """The GraphQL equivalent of a 404 softened into a 200: the failure is in
    the body, and every status-based check reports success."""
    target = tmp_path / "gql.toml"
    target.write_text(GQL_SUITE.format(base=mock.base_url), encoding="utf-8")

    record = Runner(load(target)).execute().records[0]
    assert record.candidate["status"] == 200  # nothing in the status says anything
    failed = {c["name"]: c for c in record.checks if not c["passed"]}
    assert "graphql_errors" in failed
    assert "Cannot resolve field" in failed["graphql_errors"]["detail"]
    assert record.verdict == FAIL


def test_drift_is_reported_under_data_and_errors_are_not_duplicated(
    tmp_path: Path, mock: MockServer
) -> None:
    target = tmp_path / "gql.toml"
    target.write_text(GQL_SUITE.format(base=mock.base_url), encoding="utf-8")

    record = Runner(load(target)).execute().records[0]
    drifts = {d["path"]: d for d in record.drifts}
    assert drifts["$.data.products[].price"]["kind"] == "TYPE_CHANGED"
    assert drifts["$.data.products[].stock"]["kind"] == "FIELD_REMOVED"
    # The dedicated check owns `$.errors`; repeating it as "additive; harmless"
    # would have the report contradict itself.
    assert not any(path.startswith("$.errors") for path in drifts)


def test_a_graphql_mutation_still_needs_both_switches(tmp_path: Path, mock: MockServer) -> None:
    body = GQL_SUITE.format(base=mock.base_url).replace(
        'graphql = "query Products { products { id title price stock } }"',
        'graphql = "mutation { createOrder { id status } }"',
    )
    target = tmp_path / "mut.toml"
    target.write_text(body, encoding="utf-8")

    # Refused in preflight, before a single packet: a mutation is a write
    # whatever the transport calls it.
    with pytest.raises(SafetyError, match="not marked `mutating = true`"):
        Runner(load(target)).execute()
