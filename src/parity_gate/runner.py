"""Orchestration: run every case against both targets and judge the result.

Order of operations per case, and why:

1. **Safety.** Method and URL are checked before a single packet leaves.
2. **Sample both sides N times.** Stability is measured before anything is
   compared, so a difference produced by a flaky endpoint is labelled as such
   instead of being reported as a regression.
3. **Infer both contracts from all samples.** Optionality can only be observed
   across several responses; one sample cannot tell "always present" from
   "present this time".
4. **Classify drift, then diff values, then run the case's own assertions.**
5. **Seal a record.** Findings are worth exactly as much as their evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from parity_gate import evidence, flaky
from parity_gate.differ import Difference, diff
from parity_gate.evidence import ERROR, FAIL, PASS, WARN, Record, Run
from parity_gate.httpclient import Client, Response
from parity_gate.safety import SafetyError, check_method, check_url
from parity_gate.schema import Schema, Severity, compare, infer, render_path
from parity_gate.suite import Case, Suite, Target

_SUBSCRIPT = re.compile(r"\[[^\]]*\]")


@dataclass
class Probe:
    """Everything observed from repeatedly calling one target for one case."""

    responses: list[Response] = field(default_factory=list)
    stability: flaky.Stability | None = None
    schema: Schema = field(default_factory=Schema)
    headers_sent: dict[str, str] = field(default_factory=dict)

    @property
    def first(self) -> Response | None:
        return self.responses[0] if self.responses else None

    @property
    def payload(self) -> Any:
        return self.responses[0].json_body if self.responses else None


class Runner:
    """Executes a :class:`~parity_gate.suite.Suite` and produces a :class:`Run`."""

    def __init__(self, suite: Suite, *, on_case: Any = None) -> None:
        self.suite = suite
        self.client = Client(suite.policy)
        self.on_case = on_case

    def preflight(self) -> None:
        """Validate every URL and method before the first packet leaves.

        Discovering on case 40 of 60 that the run was pointed at the wrong host
        is discovering it 39 cases too late.
        """
        for case in self.suite.cases:
            check_method(case.method, mutating=case.mutating, policy=self.suite.policy)
            for target in (self.suite.baseline, self.suite.candidate):
                check_url(target.url_for(case.path), self.suite.policy)

    def execute(self) -> Run:
        suite = self.suite
        self.preflight()
        run = Run(
            run_id=evidence.new_run_id(),
            suite_name=suite.name,
            suite_path=str(suite.source),
            suite_sha256=evidence.sha256_text(Path(suite.source).read_text(encoding="utf-8")),
            baseline_url=suite.baseline.base_url,
            candidate_url=suite.candidate.base_url,
            policy=suite.policy.to_dict(),
        )

        for case in suite.cases:
            record = self._run_case(case)
            run.add(record)
            if self.on_case:
                self.on_case(case, record)

        run.finished_at = evidence.utc_now()
        return run

    # -- one case -----------------------------------------------------------

    def _run_case(self, case: Case) -> Record:
        record = Record(case=case.to_dict(), verdict=PASS)

        try:
            check_method(case.method, mutating=case.mutating, policy=self.suite.policy)
            baseline = self._probe(self.suite.baseline, case)
            candidate = self._probe(self.suite.candidate, case)
        except SafetyError as exc:
            record.verdict = ERROR
            record.error = f"refused by safety policy: {exc}"
            return record

        record.baseline = self._exchange(baseline)
        record.candidate = self._exchange(candidate)
        record.stability = {
            "baseline": baseline.stability.to_dict() if baseline.stability else {},
            "candidate": candidate.stability.to_dict() if candidate.stability else {},
        }

        transport = self._transport_error(baseline, candidate)
        if transport:
            record.verdict = ERROR
            record.error = transport
            return record

        # A diff taken from an endpoint that answers differently on every call
        # is not evidence of anything. Say so instead of reporting the noise as
        # a regression and sending someone to chase it.
        unstable = [
            (label, probe.stability.verdict)
            for label, probe in (("baseline", baseline), ("candidate", candidate))
            if probe.stability and not probe.stability.trustworthy
        ]
        if unstable:
            record.checks = self._checks(case, candidate)
            record.error = (
                "parity comparison skipped: "
                + ", ".join(f"{label} is {verdict}" for label, verdict in unstable)
                + f" across {self.suite.repeats_for(case)} identical calls. "
                "Stabilise the endpoint before trusting any diff taken from it."
            )
            record.verdict = FAIL if any(not c["passed"] for c in record.checks) else WARN
            return record

        drifts = compare(baseline.schema, candidate.schema)
        record.drifts = [d.to_dict() for d in drifts]
        record.schema = {
            "baseline": baseline.schema.to_dict(),
            "candidate": candidate.schema.to_dict(),
        }

        # A type change reported once as drift does not need to be reported
        # again for every item that carries the field.
        differences, suppressed = _dedupe_against_drift(
            diff(baseline.payload, candidate.payload, self.suite.diff_options(case)), drifts
        )
        record.differences = [d.to_dict() for d in differences]
        record.differences_suppressed = suppressed

        record.checks = self._checks(case, candidate)
        record.verdict = self._verdict(record, drifts, differences, baseline, candidate)
        return record

    def _probe(self, target: Target, case: Case) -> Probe:
        """Call one target ``repeats`` times and summarise what came back."""
        probe = Probe()
        headers = {**target.resolved_headers(), **case.headers}
        probe.headers_sent = headers
        url = target.url_for(case.path)

        for _ in range(self.suite.repeats_for(case)):
            probe.responses.append(
                self.client.request(case.method, url, headers=headers, body=case.body)
            )

        payloads = [r.json_body for r in probe.responses]
        probe.schema = infer(*[p for p in payloads if p is not None])
        probe.stability = flaky.classify(
            statuses=[r.status for r in probe.responses],
            payloads=payloads,
            latencies_ms=[r.elapsed_ms for r in probe.responses],
            options=self.suite.diff_options(case),
        )
        return probe

    @staticmethod
    def _exchange(probe: Probe) -> dict[str, Any]:
        if not probe.first:
            return {}
        return evidence.capture_exchange(probe.first, probe.headers_sent)

    @staticmethod
    def _transport_error(baseline: Probe, candidate: Probe) -> str | None:
        for label, probe in (("baseline", baseline), ("candidate", candidate)):
            first = probe.first
            if first is None:
                return f"{label}: no response captured"
            if first.transport_error:
                return f"{label} did not answer: {first.transport_error}"
        return None

    def _checks(self, case: Case, candidate: Probe) -> list[dict[str, Any]]:
        """The case's own assertions, evaluated against the candidate."""
        results: list[dict[str, Any]] = []
        response = candidate.first
        assert response is not None  # guarded by _transport_error

        if case.expect_status:
            results.append(
                _check(
                    "status",
                    response.status in case.expect_status,
                    f"expected {case.expect_status}, got {response.status}",
                )
            )

        if response.json_error and case.method != "HEAD":
            results.append(
                _check("json", False, f"response body is not JSON: {response.json_error}")
            )

        present = {render_path(path) for path in candidate.schema.fields}
        for required in case.required_fields:
            results.append(
                _check(
                    "required_field",
                    required in present,
                    f"{required} is missing from the response",
                )
            )
        for forbidden in case.forbidden_fields:
            results.append(
                _check(
                    "forbidden_field",
                    forbidden not in present,
                    f"{forbidden} must not be exposed but is present",
                )
            )

        if case.max_latency_ms is not None and candidate.stability:
            p50 = candidate.stability.p50_ms
            results.append(
                _check(
                    "latency",
                    p50 <= case.max_latency_ms,
                    f"p50 {p50}ms exceeds the {case.max_latency_ms}ms budget",
                )
            )

        return results

    @staticmethod
    def _verdict(
        record: Record,
        drifts: list[Any],
        differences: list[Difference],
        baseline: Probe,
        candidate: Probe,
    ) -> str:
        if any(not check["passed"] for check in record.checks):
            return FAIL
        if any(d.severity is Severity.BREAKING for d in drifts):
            return FAIL
        if any(d.kind != "ORDER_ONLY" for d in differences):
            return FAIL
        if drifts or differences:
            return WARN
        if any(
            probe.stability and probe.stability.verdict == flaky.VOLATILE_BODY
            for probe in (baseline, candidate)
        ):
            return WARN
        return PASS


def _check(name: str, passed: bool, failure_detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": "" if passed else failure_detail}


#: Which value-level difference each contract-drift finding already explains.
_EXPLAINED_BY = {
    "TYPE_CHANGED": {"TYPE"},
    "TYPE_WIDENED": {"TYPE"},
    "TYPE_NARROWED": {"TYPE"},
    "NULLABLE_ADDED": {"TYPE"},
    "FIELD_REMOVED": {"MISSING_IN_CANDIDATE"},
    "FIELD_NOW_OPTIONAL": {"MISSING_IN_CANDIDATE"},
    "FIELD_ADDED": {"EXTRA_IN_CANDIDATE"},
    "FIELD_NOW_ALWAYS_PRESENT": {"EXTRA_IN_CANDIDATE"},
}


def _dedupe_against_drift(
    differences: list[Difference], drifts: list[Any]
) -> tuple[list[Difference], int]:
    """Drop value differences that the contract-drift section already states.

    Four products whose ``price`` changed type produce four identical value
    differences and one drift finding. The drift finding is the useful one; the
    rest is volume. The count of what was folded away is kept and reported, so
    nothing disappears without being accounted for.
    """
    explained: set[tuple[str, str]] = set()
    for drift in drifts:
        for kind in _EXPLAINED_BY.get(drift.kind, ()):  # type: ignore[attr-defined]
            explained.add((drift.path, kind))  # type: ignore[attr-defined]

    kept: list[Difference] = []
    folded = 0
    for difference in differences:
        generalised = _SUBSCRIPT.sub("[]", difference.path)
        if (generalised, difference.kind) in explained:
            folded += 1
            continue
        kept.append(difference)
    return kept, folded
