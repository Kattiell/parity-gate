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
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from parity_gate import contracts, evidence, flaky, graphql, rules, waivers
from parity_gate.differ import Difference, DiffOptions, diff
from parity_gate.evidence import ERROR, FAIL, PASS, WARN, Record, Run
from parity_gate.httpclient import Client, Response
from parity_gate.redaction import redact_text
from parity_gate.safety import SafetyError, check_method, check_url
from parity_gate.schema import Schema, Severity, compare, infer, render_path
from parity_gate.suite import Case, Suite, Target

_SUBSCRIPT = re.compile(r"\[[^\]]*\]")


class ContractRecordingError(Exception):
    """A contract could not be recorded because the source did not fully answer."""


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

    def __init__(self, suite: Suite, *, on_case: Any = None, revision: str | None = None) -> None:
        self.suite = suite
        self.on_case = on_case
        self.revision = revision
        # Loaded lazily: `record` has to run before the file exists.
        self.contract: contracts.Contract | None = None
        # One client per worker thread. urllib openers make no thread-safety
        # promise, and sharing one across workers is the kind of bug that shows
        # up as a flaky result in the tool that exists to diagnose flakiness.
        self._local = threading.local()
        self._clients: list[Client] = []

    @property
    def client(self) -> Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._local.client = Client(self.suite.policy)
            self._clients.append(client)
        return client

    def close(self) -> None:
        """Release every pooled connection this run opened.

        Worker threads end when the pool shuts down and their connections would
        be collected eventually, but "eventually" is not a socket lifetime a
        long CI run should depend on.
        """
        for client in self._clients:
            client.close()
        self._clients.clear()

    def _map_cases(self, work: Any, on_result: Any = None) -> list[Any]:
        """Run ``work(case)`` over every case and return the results in suite order.

        Results are consumed in submission order rather than as they complete,
        so a parallel run produces exactly the same report, hash chain and
        console output as a sequential one. A gate whose output depends on
        scheduling is a gate nobody can diff between runs.
        """
        workers = max(1, self.suite.policy.workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="parity") as pool:
            futures = [(case, pool.submit(work, case)) for case in self.suite.cases]
            results = []
            for case, future in futures:
                result = future.result()
                results.append(result)
                if on_result:
                    on_result(case, result)
        return results

    def _load_contract(self) -> None:
        if self.suite.baseline.is_recorded and self.contract is None:
            path = self.suite.contract_path
            assert path is not None
            self.contract = contracts.load(path)

    def preflight(self) -> None:
        """Validate every URL and method before the first packet leaves.

        Discovering on case 40 of 60 that the run was pointed at the wrong host
        is discovering it 39 cases too late.
        """
        live = [t for t in (self.suite.baseline, self.suite.candidate) if not t.is_recorded]
        for target in live:
            # Resolving here means a missing credential is a refusal at the
            # door, not an exception three cases into a run that has already
            # printed a header and looks like it is working.
            target.resolved_headers()
        for case in self.suite.cases:
            check_method(
                case.method,
                mutating=case.mutating,
                policy=self.suite.policy,
                reads_only=case.reads_only,
            )
            for target in live:
                check_url(target.url_for(case.path), self.suite.policy)

    def _new_run(self) -> Run:
        suite = self.suite
        return Run(
            run_id=evidence.new_run_id(),
            suite_name=suite.name,
            suite_path=str(suite.source),
            suite_sha256=evidence.sha256_text(Path(suite.source).read_text(encoding="utf-8")),
            baseline_url=(
                f"recorded contract: {suite.baseline.snapshot}"
                if suite.baseline.is_recorded
                else suite.baseline.base_url
            ),
            candidate_url=suite.candidate.label,
            policy=suite.policy.to_dict(),
            mode="contract" if suite.baseline.is_recorded else "differential",
            context=evidence.ci_context(self.revision),
            sampling={
                "repeats": suite.repeats,
                "repeat_interval_ms": suite.repeat_interval_ms,
            },
        )

    def record_contract(self, on_case: Any = None) -> contracts.Contract:
        """Probe the live candidate and capture the shape it has today.

        This is how a team with a single API gets a baseline: record once,
        review the file, commit it. Stability is recorded alongside each case,
        because a contract taken from an endpoint that answers differently on
        every call is worth knowing about before it is trusted as a reference.
        """
        self.preflight()
        contract = contracts.Contract(
            suite_name=self.suite.name,
            source_url=self.suite.candidate.base_url,
            recorded_at=contracts.now(),
            note="Shape only: types, requiredness and observed statuses. No values.",
        )

        def probe_one(case: Case) -> Probe:
            probe = self._probe(self.suite.candidate, case)
            if probe.first is None or probe.first.transport_error:
                raise ContractRecordingError(
                    f"case {case.id!r} did not answer: "
                    f"{probe.first.transport_error if probe.first else 'no response'}. "
                    "A contract recorded from a half-reachable service is worse than none."
                )
            return probe

        for case, probe in zip(self.suite.cases, self._map_cases(probe_one, on_case), strict=True):
            contract.cases[case.id] = contracts.RecordedCase(
                schema=probe.schema,
                statuses=sorted({r.status for r in probe.responses if r.status is not None}),
                stability=probe.stability.verdict if probe.stability else "UNKNOWN",
            )
        self.close()
        return contract

    def measure_stability(self, on_case: Any = None) -> Run:
        """Call every case repeatedly and report only how steady the answers are.

        No baseline, no contract, no comparison, and the suite's own assertions
        are deliberately not evaluated: this measures the endpoints, not the
        expectations. It is what to run before writing assertions against an
        API, because an endpoint that answers differently on identical calls
        will make any suite built on it look broken at random.
        """
        self.preflight()
        run = self._new_run()

        def measure_one(case: Case) -> Record:
            record = Record(case=case.to_dict(), verdict=PASS)
            try:
                check_method(
                    case.method,
                    mutating=case.mutating,
                    policy=self.suite.policy,
                    reads_only=case.reads_only,
                )
                probe = self._probe(self.suite.candidate, case)
            except SafetyError as exc:
                record.verdict = ERROR
                record.error = f"refused by safety policy: {exc}"
                return record

            record.candidate = _exchange(probe)
            record.stability = {
                "baseline": {},
                "candidate": probe.stability.to_dict() if probe.stability else {},
            }
            if probe.first is None or probe.first.transport_error:
                record.verdict = ERROR
                record.error = _transport_error(None, probe)
            else:
                verdict = probe.stability.verdict if probe.stability else flaky.STABLE
                if verdict in {flaky.FLAKY_STATUS, flaky.FLAKY_SHAPE}:
                    record.verdict = FAIL
                elif verdict == flaky.VOLATILE_BODY:
                    record.verdict = WARN
            return record

        def announce(case: Case, record: Record) -> None:
            if on_case:
                on_case(case, record)

        for record in self._map_cases(_contained(measure_one), announce):
            run.add(record)

        run.finished_at = evidence.utc_now()
        self.close()
        return run

    def execute(self) -> Run:
        self.preflight()
        self._load_contract()
        run = self._new_run()

        for record in self._map_cases(
            _contained(self._run_case), lambda case, rec: self.on_case and self.on_case(case, rec)
        ):
            run.add(record)

        if self.suite.waivers:
            run.waivers = waivers.summarise(self.suite.waivers, run.records, waivers.today())
        run.finished_at = evidence.utc_now()
        self.close()
        return run

    # -- one case -----------------------------------------------------------

    def _run_case(self, case: Case) -> Record:
        if self.contract is not None:
            return self._run_case_against_contract(case)
        return self._run_case_against_live_baseline(case)

    def _run_case_against_contract(self, case: Case) -> Record:
        assert self.contract is not None
        try:
            check_method(
                case.method,
                mutating=case.mutating,
                policy=self.suite.policy,
                reads_only=case.reads_only,
            )
            candidate = self._probe(self.suite.candidate, case)
        except SafetyError as exc:
            return _refused(case, exc)
        return judge_contract(self.suite, case, self.contract, candidate)

    def _run_case_against_live_baseline(self, case: Case) -> Record:
        try:
            check_method(
                case.method,
                mutating=case.mutating,
                policy=self.suite.policy,
                reads_only=case.reads_only,
            )
            baseline = self._probe(self.suite.baseline, case)
            candidate = self._probe(self.suite.candidate, case)
        except SafetyError as exc:
            return _refused(case, exc)
        return judge_differential(self.suite, case, baseline, candidate)

    def sample(self, case: Case, side: str = "candidate") -> Response:
        """One call to one live target, under the same safety policy as a run.

        Used by ``selftest`` to take real responses as the reference its
        faults are injected into. Call :meth:`preflight` first.
        """
        target = self.suite.candidate if side == "candidate" else self.suite.baseline
        if target.is_recorded:
            raise SafetyError(
                f"targets.{side} is a recorded contract; there is nothing live to sample"
            )
        check_method(
            case.method,
            mutating=case.mutating,
            policy=self.suite.policy,
            reads_only=case.reads_only,
        )
        headers = {**target.resolved_headers(), **case.headers}
        return self.client.request(
            case.method, target.url_for(case.path), headers=headers, body=case.request_body()
        )

    def _probe(self, target: Target, case: Case) -> Probe:
        """Call one target ``repeats`` times and summarise what came back."""
        headers = {**target.resolved_headers(), **case.headers}
        url = target.url_for(case.path)
        responses: list[Response] = []
        for index in range(self.suite.repeats_for(case)):
            if index and self.suite.repeat_interval_ms:
                time.sleep(self.suite.repeat_interval_ms / 1000)
            responses.append(
                self.client.request(case.method, url, headers=headers, body=case.request_body())
            )
        return summarise(responses, self.suite.diff_options(case), headers)


# -- judging ------------------------------------------------------------------
#
# Everything below is a pure function of what was observed. No sockets, no
# clock, no suite file on disk: that is what lets `selftest` put mutated
# responses through exactly the code that judges real ones, instead of through
# a copy of it that could drift.


def summarise(
    responses: list[Response], options: DiffOptions, headers_sent: dict[str, str] | None = None
) -> Probe:
    """Infer the shape and classify the stability of repeated responses."""
    payloads = [r.json_body for r in responses]
    return Probe(
        responses=list(responses),
        schema=infer(*[p for p in payloads if p is not None]),
        stability=flaky.classify(
            statuses=[r.status for r in responses],
            payloads=payloads,
            latencies_ms=[r.elapsed_ms for r in responses],
            options=options,
        ),
        headers_sent=dict(headers_sent or {}),
    )


def judge_contract(
    suite: Suite, case: Case, contract: contracts.Contract, candidate: Probe
) -> Record:
    """Gate one case against the shape recorded for it earlier.

    Only the contract is compared, never values: the recording holds types
    and requiredness, not data, on purpose. Statuses are part of the
    contract too, so an endpoint that used to answer 404 and now answers
    200 is caught without anyone having written that assertion.
    """
    record = Record(case=case.to_dict(), verdict=PASS)
    recorded = contract.cases.get(case.id)

    record.candidate = _exchange(candidate)
    record.baseline = {
        "source": "recorded contract",
        "path": str(suite.contract_path),
        "recorded_at": contract.recorded_at,
        "recorded_from": contract.source_url,
    }
    record.stability = {
        "baseline": {},
        "candidate": candidate.stability.to_dict() if candidate.stability else {},
    }

    if candidate.first is None or candidate.first.transport_error:
        record.verdict = ERROR
        record.error = _transport_error(None, candidate)
        return record

    if recorded is None:
        record.checks = evaluate_checks(case, candidate)
        record.error = (
            f"case {case.id!r} has no entry in the recorded contract, so its shape is "
            "not gated. Re-record after reviewing: parity-gate record --suite <suite>"
        )
        record.verdict = _settle(suite, record, floor=WARN)
        return record

    if candidate.stability and not candidate.stability.trustworthy:
        record.checks = evaluate_checks(case, candidate)
        record.error = (
            f"contract check skipped: candidate is {candidate.stability.verdict} across "
            f"{suite.repeats_for(case)} identical calls. Stabilise the endpoint first."
        )
        record.verdict = _settle(suite, record, floor=WARN)
        return record

    drifts, unchecked = compare(recorded.schema, candidate.schema)
    drifts = _fold_graphql_errors(drifts, case)
    record.drifts = [d.to_dict() for d in drifts]
    record.unchecked_paths = unchecked
    record.schema = {
        "baseline": recorded.schema.to_dict(),
        "candidate": candidate.schema.to_dict(),
    }

    record.checks = evaluate_checks(case, candidate)
    record.checks.extend(_status_contract_checks(recorded, candidate))
    record.differences_suppressed = 0
    record.verdict = _settle(suite, record)
    return record


def judge_differential(suite: Suite, case: Case, baseline: Probe, candidate: Probe) -> Record:
    """Gate one case by comparing a live candidate against a live baseline."""
    record = Record(case=case.to_dict(), verdict=PASS)

    record.baseline = _exchange(baseline)
    record.candidate = _exchange(candidate)
    record.stability = {
        "baseline": baseline.stability.to_dict() if baseline.stability else {},
        "candidate": candidate.stability.to_dict() if candidate.stability else {},
    }

    transport = _transport_error(baseline, candidate)
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
        record.checks = evaluate_checks(case, candidate)
        record.error = (
            "parity comparison skipped: "
            + ", ".join(f"{label} is {verdict}" for label, verdict in unstable)
            + f" across {suite.repeats_for(case)} identical calls. "
            "Stabilise the endpoint before trusting any diff taken from it."
        )
        record.verdict = _settle(suite, record, floor=WARN)
        return record

    drifts, unchecked = compare(baseline.schema, candidate.schema)
    drifts = _fold_graphql_errors(drifts, case)
    record.drifts = [d.to_dict() for d in drifts]
    record.unchecked_paths = unchecked
    record.schema = {
        "baseline": baseline.schema.to_dict(),
        "candidate": candidate.schema.to_dict(),
    }

    # A type change reported once as drift does not need to be reported
    # again for every item that carries the field.
    differences, suppressed = _dedupe_against_drift(
        diff(baseline.payload, candidate.payload, suite.diff_options(case)), drifts
    )
    record.differences = [d.to_dict() for d in differences]
    record.differences_suppressed = suppressed

    record.checks = evaluate_checks(case, candidate)
    record.checks.extend(_status_parity_checks(baseline, candidate))
    record.verdict = _settle(suite, record)
    return record


def _settle(suite: Suite, record: Record, *, floor: str = PASS) -> str:
    """Apply the suite's waivers to the findings, then decide the verdict."""
    waivers.apply(record, suite.waivers, waivers.today())
    return decide(record, floor=floor)


def decide(record: Record, *, floor: str = PASS) -> str:
    """The verdict rules, in one place, for every mode.

    A failed assertion, a breaking drift or a value difference other than
    reordering fails the case. Anything else that was noticed (a non-breaking
    drift, a reordering, a volatile body, a finding a waiver accepted) is a
    warning. ``floor`` is the best a case can do: a case whose comparison was
    skipped cannot pass, only warn.
    """
    checks = [c for c in record.checks if not c.get("waiver")]
    drifts = [d for d in record.drifts if not d.get("waiver")]
    differences = [d for d in record.differences if not d.get("waiver")]
    if any(not check["passed"] for check in checks):
        return FAIL
    if any(drift["severity"] == Severity.BREAKING.value for drift in drifts):
        return FAIL
    if any(difference["kind"] != "ORDER_ONLY" for difference in differences):
        return FAIL
    volatile = any(
        isinstance(side, dict) and side.get("verdict") == flaky.VOLATILE_BODY
        for side in record.stability.values()
    )
    waived = any(not c["passed"] for c in record.checks if c.get("waiver"))
    if record.drifts or record.differences or volatile or waived:
        return WARN
    return floor


def evaluate_checks(case: Case, candidate: Probe) -> list[dict[str, Any]]:
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
        results.append(_check("json", False, f"response body is not JSON: {response.json_error}"))

    present = {render_path(path) for path in candidate.schema.fields}
    for required in case.required_fields:
        results.append(
            _check(
                "required_field",
                required in present,
                f"{required} is missing from the response",
                path=required,
            )
        )
    for forbidden in case.forbidden_fields:
        results.append(
            _check(
                "forbidden_field",
                forbidden not in present,
                f"{forbidden} must not be exposed but is present",
                path=forbidden,
            )
        )

    if case.is_graphql:
        found = graphql.errors_in(response.json_body)
        if case.expect_graphql_errors:
            results.append(
                _check("graphql_errors", bool(found), "expected GraphQL errors, got none")
            )
        else:
            results.append(
                _check(
                    "graphql_errors",
                    not found,
                    f"the response carries GraphQL errors: {graphql.describe(found)}",
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


def _exchange(probe: Probe) -> dict[str, Any]:
    if not probe.first:
        return {}
    return evidence.capture_exchange(probe.first, probe.headers_sent)


def _transport_error(baseline: Probe | None, candidate: Probe) -> str | None:
    pairs = [("candidate", candidate)]
    if baseline is not None:
        pairs.insert(0, ("baseline", baseline))
    for label, probe in pairs:
        first = probe.first
        if first is None:
            return f"{label}: no response captured"
        if first.transport_error:
            return f"{label} did not answer: {first.transport_error}"
    return None


def _refused(case: Case, exc: SafetyError) -> Record:
    return Record(case=case.to_dict(), verdict=ERROR, error=f"refused by safety policy: {exc}")


def _contained(work: Callable[[Case], Record]) -> Callable[[Case], Record]:
    """Turn an unexpected exception in one case into an ``ERROR`` record for it.

    An exception escaping a worker thread used to surface at ``future.result()``
    and end the whole run: no report, no evidence, and every case that had
    already finished thrown away with it. A defect in the tool is still a
    defect, and it still fails the gate, but it is reported against the case
    that triggered it instead of taking the other cases down.
    """

    def guarded(case: Case) -> Record:
        try:
            return work(case)
        except Exception as exc:
            frames = traceback.extract_tb(exc.__traceback__)
            where = f" at {Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
            return Record(
                case=case.to_dict(),
                verdict=ERROR,
                error=redact_text(
                    f"parity-gate raised {type(exc).__name__}{where}: {exc}. This is a defect "
                    "in the tool, not a finding about the API; the other cases were unaffected."
                ),
            )

    return guarded


def _check(name: str, passed: bool, failure_detail: str, path: str = "$") -> dict[str, Any]:
    return {
        "rule": rules.rule_id(rules.CHECK, name),
        "name": name,
        "path": path,
        "passed": passed,
        "detail": "" if passed else failure_detail,
    }


def _status_parity_checks(baseline: Probe, candidate: Probe) -> list[dict[str, Any]]:
    """The status code is part of the contract, so a divergence is a failure.

    Without this, a baseline answering 201 and a candidate answering 200 with an
    identical body is reported as PASS: no drift, no value difference, and no
    assertion unless somebody predicted that exact change and wrote
    ``expect_status``. Predicting the change is the thing this tool is supposed
    to make unnecessary.
    """
    seen_base = sorted({r.status for r in baseline.responses if r.status is not None})
    seen_cand = sorted({r.status for r in candidate.responses if r.status is not None})
    if not seen_base or not seen_cand:
        return []
    return [
        _check(
            "status_parity",
            seen_base == seen_cand,
            f"baseline answered {seen_base}, candidate answered {seen_cand}",
        )
    ]


def _status_contract_checks(
    recorded: contracts.RecordedCase, candidate: Probe
) -> list[dict[str, Any]]:
    """The status an endpoint answers with is part of its contract.

    Recording it means the classic "404 quietly became 200" is caught on a case
    where nobody thought to write ``expect_status``.
    """
    if not recorded.statuses:
        return []
    seen = sorted({r.status for r in candidate.responses if r.status is not None})
    unexpected = [status for status in seen if status not in recorded.statuses]
    return [
        _check(
            "recorded_status",
            not unexpected,
            f"recorded contract answers {recorded.statuses}, this run answered {seen}",
        )
    ]


def _fold_graphql_errors(drifts: list[Any], case: Case) -> list[Any]:
    """Drop drift findings about ``$.errors`` on a GraphQL case.

    The dedicated `graphql_errors` check already reports them, and reports them
    correctly. Leaving them in the drift table as well makes the report say two
    contradictory things about the same fact: "additive; harmless for
    consumers that ignore unknown fields" next to a failed assertion about
    exactly that field.
    """
    if not case.is_graphql:
        return drifts
    return [d for d in drifts if not d.path.startswith("$.errors")]


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
