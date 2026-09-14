"""The selftest measures the gate, so it has to be shown to notice a blind one.

A fault-injection harness that reports 100% no matter what is worse than none:
it turns a guess into a number. So besides checking that the real gate scores
well, these tests break the gate on purpose and assert the score drops, and
break a control on purpose and assert a false alarm is counted.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from parity_gate import schema, selftest
from parity_gate.cli import EXIT_GATE_FAILED, EXIT_OK, main
from parity_gate.suite import load

SUITE = """
name = "selftest-unit"
[targets.baseline]
base_url = "http://127.0.0.1:9/legacy"
[targets.candidate]
base_url = "http://127.0.0.1:9/next"
[policy]
allowed_hosts = ["127.0.0.1"]
allow_private_networks = true
repeats = 2
mask_paths = ["$.meta.requestId"]
[policy.array_keys]
"$.products" = "id"

[[cases]]
id = "CAT-001"
title = "listing"
path = "/products"
required_fields = ["$.products"]
"""

PAYLOAD = {
    "meta": {"requestId": "r-1"},
    "total": 2,
    "products": [
        {"id": 1, "title": "Torx", "price": 19.9, "inStock": True},
        {"id": 2, "title": "Drill", "price": 459.0, "inStock": False},
    ],
}


@pytest.fixture
def suite(tmp_path: Path):  # type: ignore[no-untyped-def]
    target = tmp_path / "suite.toml"
    target.write_text(SUITE, encoding="utf-8")
    return load(target)


def sample(suite, payload=PAYLOAD, status=200):  # type: ignore[no-untyped-def]
    return selftest.Sample(case=suite.cases[0], status=status, payload=copy.deepcopy(payload))


def test_the_gate_catches_the_whole_fault_model_and_tolerates_every_control(suite) -> None:  # type: ignore[no-untyped-def]
    report = selftest.run(suite, [sample(suite)])

    for mode in selftest.MODES:
        caught, faults = report.detection(mode)
        alarms, controls = report.false_alarms(mode)
        assert faults > 0 and controls > 0
        assert caught == faults, [m.to_dict() for m in report.of(mode, selftest.DETECT)]
        assert alarms == 0, [m.to_dict() for m in report.of(mode, selftest.TOLERATE)]
    assert report.errors == []


def test_a_gate_blind_to_type_changes_scores_below_full_detection(
    suite, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The whole point: break the judge and the number has to move."""
    monkeypatch.setattr(schema, "_compare_field", lambda *args: [])
    report = selftest.run(suite, [sample(suite)], modes=(selftest.CONTRACT,))

    missed = {m.operator for m in report.mutants if m.outcome == selftest.MISSED}
    caught, faults = report.detection(selftest.CONTRACT)
    assert {"TYPE_SWAP", "NULL_INJECT"} <= missed
    assert caught < faults


def test_a_gate_that_fails_on_everything_is_counted_as_false_alarms(
    suite, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Full detection is easy if every change fails the build. The controls are
    what stop that from scoring well."""
    import parity_gate.runner as runner_module

    real = runner_module.evaluate_checks

    def paranoid(case, candidate):  # type: ignore[no-untyped-def]
        return [*real(case, candidate), runner_module._check("status", False, "always")]

    monkeypatch.setattr(runner_module, "evaluate_checks", paranoid)
    report = selftest.run(suite, [sample(suite)], modes=(selftest.CONTRACT,))

    alarms, controls = report.false_alarms(selftest.CONTRACT)
    assert controls > 0 and alarms == controls


def test_every_mutant_is_one_fault_at_one_site_and_the_reference_is_untouched(suite) -> None:  # type: ignore[no-untyped-def]
    reference = sample(suite)
    before = json.dumps(reference.payload, sort_keys=True)
    type_swap = next(op for op in selftest.operators(suite) if op.name == "TYPE_SWAP")

    mutants = list(type_swap.apply(reference, suite))

    assert json.dumps(reference.payload, sort_keys=True) == before
    sites = [site for site, _, _ in mutants]
    # One site per distinct path: the first product only, never every product.
    assert "$.products[0].price" in sites and "$.products[1].price" not in sites
    # Masked paths are controls, not faults.
    assert "$.meta.requestId" not in sites


def test_reordering_is_a_fault_when_the_suite_says_order_is_the_contract(tmp_path: Path) -> None:
    target = tmp_path / "ordered.toml"
    target.write_text(
        SUITE.replace("repeats = 2", "repeats = 2\nignore_array_order = false"), encoding="utf-8"
    )
    ordered = load(target)
    expectations = {op.name: op.expect for op in selftest.operators(ordered)}
    assert expectations["REORDER"][selftest.DIFFERENTIAL] == selftest.DETECT
    assert expectations["REORDER"][selftest.CONTRACT] == selftest.TOLERATE


def test_a_control_that_would_break_the_cases_own_assertion_is_not_counted(suite) -> None:  # type: ignore[no-untyped-def]
    """Emptying `products` is a control in contract mode. A case that requires a
    field inside the items has asked for exactly the data emptying removes, so
    the control does not apply to it and must not be scored as an alarm."""
    suite.cases[0].required_fields = ["$.products[].id"]
    report = selftest.run(suite, [sample(suite)], modes=(selftest.CONTRACT,))
    emptied = [m for m in report.mutants if m.operator == "COLLECTION_EMPTY"]
    assert emptied == []


def test_the_cli_selftest_on_the_demo_passes_and_writes_every_mutant(tmp_path: Path) -> None:
    out = tmp_path / "selftest.json"
    code = main(["selftest", "--demo", "--quiet", "--json", str(out)])
    assert code == EXIT_OK

    data = json.loads(out.read_text(encoding="utf-8"))
    for mode in selftest.MODES:
        assert data["summary"][mode]["detection_rate"] == 1.0
        assert data["summary"][mode]["false_alarm_rate"] == 0.0
    assert data["errors"] == 0
    assert data["mutants"]


def test_the_cli_selftest_fails_the_build_when_detection_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schema, "_compare_field", lambda *args: [])
    assert main(["selftest", "--demo", "--quiet"]) == EXIT_GATE_FAILED
