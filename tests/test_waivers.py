"""Waivers accept a finding on the record: with an owner, a reason and a date
after which they stop working. Each of those properties is pinned here, because
a waiver that quietly outlives its date is a deleted check."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from parity_gate import waivers
from parity_gate.cli import EXIT_GATE_FAILED, main
from parity_gate.evidence import FAIL, PASS, WARN, Record
from parity_gate.runner import decide
from parity_gate.suite import SuiteError, load

TODAY = date(2026, 9, 13)

MINIMAL = """
name = "w"
[targets.baseline]
base_url = "http://127.0.0.1:9/legacy"
[targets.candidate]
base_url = "http://127.0.0.1:9/next"
[policy]
allowed_hosts = ["127.0.0.1"]
[[cases]]
id = "CAT-001"
path = "/x"
"""


def entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "rule": "PG1001",
        "case": "CAT-*",
        "path": "$.products[].stock",
        "owner": "storefront",
        "expires": "2026-10-01",
        "reason": "stock moved to the availability endpoint",
    }
    base.update(overrides)
    return base


def record_with(*, drifts=(), checks=(), differences=()) -> Record:  # type: ignore[no-untyped-def]
    return Record(
        case={"id": "CAT-001"},
        verdict=PASS,
        drifts=[dict(d) for d in drifts],
        checks=[dict(c) for c in checks],
        differences=[dict(d) for d in differences],
    )


STOCK_REMOVED = {
    "rule": "PG1001",
    "path": "$.products[].stock",
    "kind": "FIELD_REMOVED",
    "severity": "breaking",
    "detail": "",
}


# -- what a waiver must carry ---------------------------------------------------


@pytest.mark.parametrize("missing", ["rule", "owner", "expires", "reason"])
def test_a_waiver_without_an_owner_an_expiry_and_a_reason_is_refused(missing: str) -> None:
    raw = entry()
    del raw[missing]
    with pytest.raises(waivers.WaiverError, match=missing):
        waivers.parse([raw], on=TODAY)


def test_a_rule_can_be_named_but_is_stored_as_its_id() -> None:
    (parsed,) = waivers.parse([entry(rule="FIELD_REMOVED")], on=TODAY)
    assert parsed.rule == "PG1001"


def test_an_unknown_rule_is_refused_rather_than_matching_nothing_forever() -> None:
    with pytest.raises(waivers.WaiverError, match="unknown rule"):
        waivers.parse([entry(rule="PG1999")], on=TODAY)


@pytest.mark.parametrize("rule", ["PG4001", "PG5001"])
def test_instability_and_unjudged_cases_cannot_be_waived(rule: str) -> None:
    with pytest.raises(waivers.WaiverError, match="cannot be waived"):
        waivers.parse([entry(rule=rule)], on=TODAY)


def test_a_waiver_dated_past_the_policy_horizon_is_refused() -> None:
    with pytest.raises(waivers.WaiverError, match="max_waiver_days"):
        waivers.parse([entry(expires="2099-01-01")], on=TODAY)
    assert waivers.parse([entry(expires="2026-12-12")], max_days=90, on=TODAY)


def test_an_already_expired_waiver_still_loads_so_it_can_be_reported() -> None:
    (parsed,) = waivers.parse([entry(expires="2026-01-01")], on=TODAY)
    assert not parsed.active(TODAY)


# -- what a waiver does to a verdict ---------------------------------------------


def test_an_active_waiver_turns_a_breaking_drift_into_a_warning_not_a_pass() -> None:
    record = record_with(drifts=[STOCK_REMOVED])
    waivers.apply(record, waivers.parse([entry()], on=TODAY), TODAY)

    assert record.drifts[0]["waiver"]["owner"] == "storefront"
    assert decide(record) == WARN


def test_a_waiver_waives_nothing_the_day_after_it_expires() -> None:
    parsed = waivers.parse([entry(expires="2026-09-13")], on=TODAY)

    on_the_day = record_with(drifts=[STOCK_REMOVED])
    waivers.apply(on_the_day, parsed, date(2026, 9, 13))
    assert decide(on_the_day) == WARN

    the_day_after = record_with(drifts=[STOCK_REMOVED])
    waivers.apply(the_day_after, parsed, date(2026, 9, 14))
    assert "waiver" not in the_day_after.drifts[0]
    assert the_day_after.drifts[0]["waiver_expired"]["expires"] == "2026-09-13"
    assert decide(the_day_after) == FAIL


def test_a_waiver_only_covers_the_rule_case_and_path_it_names() -> None:
    parsed = waivers.parse([entry()], on=TODAY)
    other_path = {**STOCK_REMOVED, "path": "$.products[].price"}
    other_rule = {**STOCK_REMOVED, "rule": "PG1004", "kind": "TYPE_CHANGED"}
    record = record_with(drifts=[other_path, other_rule])

    waivers.apply(record, parsed, TODAY)

    assert all("waiver" not in d for d in record.drifts)
    assert decide(record) == FAIL


def test_a_failed_assertion_is_matched_on_the_field_it_names() -> None:
    parsed = waivers.parse(
        [entry(rule="PG3006", path="$.products[].internalCost", case="CAT-001")], on=TODAY
    )
    exposed = {
        "rule": "PG3006",
        "name": "forbidden_field",
        "path": "$.products[].internalCost",
        "passed": False,
        "detail": "must not be exposed",
    }
    record = record_with(checks=[exposed])

    waivers.apply(record, parsed, TODAY)

    assert record.checks[0]["waiver"]["rule"] == "PG3006"
    assert decide(record) == WARN


def test_the_run_summary_separates_applied_expired_and_unused() -> None:
    parsed = waivers.parse(
        [
            entry(),
            entry(rule="PG1004", path="$.products[].price", expires="2026-01-01"),
            entry(rule="PG1003", case="NOPE-*"),
        ],
        on=TODAY,
    )
    price = {**STOCK_REMOVED, "rule": "PG1004", "path": "$.products[].price"}
    record = record_with(drifts=[STOCK_REMOVED, price])
    waivers.apply(record, parsed, TODAY)

    summary = waivers.summarise(parsed, [record], TODAY)

    assert [w["rule"] for w in summary["applied"]] == ["PG1001"]
    assert [w["rule"] for w in summary["expired"]] == ["PG1004"]
    assert [w["rule"] for w in summary["unused"]] == ["PG1003"]


# -- in a suite and a real run ---------------------------------------------------


def test_waivers_and_their_horizon_load_from_the_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(waivers, "today", lambda: TODAY)
    body = (
        MINIMAL.replace("[policy]", "[policy]\nmax_waiver_days = 400", 1)
        + '\n[[waivers]]\nrule = "PG1001"\nowner = "a"\nexpires = 2027-06-01\nreason = "r"\n'
    )
    suite_file = tmp_path / "s.toml"
    suite_file.write_text(body, encoding="utf-8")

    (loaded,) = load(suite_file).waivers
    assert loaded.expires == date(2027, 6, 1)
    assert loaded.case == "*" and loaded.path == "*"


def test_a_malformed_waiver_is_a_suite_error_that_names_it(tmp_path: Path) -> None:
    body = MINIMAL + '\n[[waivers]]\nrule = "PG1001"\nowner = "a"\nexpires = 2026-10-01\n'
    suite_file = tmp_path / "s.toml"
    suite_file.write_text(body, encoding="utf-8")
    with pytest.raises(SuiteError, match=r"\[\[waivers\]\] #1 is missing reason"):
        load(suite_file)


@pytest.mark.integration
def test_a_run_reports_waivers_in_evidence_and_sarif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parity_gate import demo
    from parity_gate.mock import start

    monkeypatch.setattr(waivers, "today", lambda: TODAY)
    suite_file = tmp_path / "suite.toml"
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "demo-catalog.json").write_bytes(
        (demo.suite_path("contract").parent / "contracts" / "demo-catalog.json").read_bytes()
    )
    server = start(0)
    try:
        text = demo.suite_path("contract").read_text(encoding="utf-8")
        text = text.replace("http://127.0.0.1:8799", server.base_url)
        text += (
            '\n[[waivers]]\nrule = "PG3006"\ncase = "SEC-001"\n'
            'path = "$.products[].internalCost"\nowner = "catalogue-team"\n'
            'expires = 2026-10-10\nticket = "CAT-4412"\nreason = "ships with serializer v3"\n'
        )
        suite_file.write_text(text, encoding="utf-8")
        code = main(
            ["run", "--suite", str(suite_file), "--evidence", str(tmp_path / "ev"), "--quiet"]
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code == EXIT_GATE_FAILED  # the other breaking drifts still fail it
    (bundle,) = list((tmp_path / "ev").iterdir())
    run = json.loads((bundle / "run.json").read_text(encoding="utf-8"))
    assert run["waivers"]["applied"][0]["ticket"] == "CAT-4412"
    assert run["summary"]["waived_findings"] == 1

    log = json.loads((bundle / "report.sarif").read_text(encoding="utf-8"))
    exposure = next(r for r in log["runs"][0]["results"] if r["ruleId"] == "PG3006")
    assert exposure["suppressions"][0]["status"] == "accepted"
    assert "catalogue-team" in exposure["suppressions"][0]["justification"]
    assert "## Waivers" in (bundle / "report.md").read_text(encoding="utf-8")
