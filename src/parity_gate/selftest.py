"""Measure the gate itself: inject known faults and count what it catches.

A gate earns trust by what it has been shown to catch, and a single
adversarial review is a snapshot of that. Functional-safety practice has a
name for the continuous version: *diagnostic coverage*, the fraction of a
defined fault model that a detection mechanism is demonstrated to detect,
measured by injecting those faults on purpose. A detector also has to be
measured the other way, because one that fires on everything catches every
fault and is still switched off in a week.

So ``selftest`` takes real responses, applies a fixed fault model to them one
site at a time, and puts every mutant through **the same judging functions a
real run uses** (:func:`parity_gate.runner.judge_differential` and
:func:`parity_gate.runner.judge_contract`), not through a copy of their rules.
Two numbers come out per mode:

``detection``
    Of the mutants a consumer could not survive, the share that made the case
    FAIL. Anything below 100% is a named fault the gate lets through.
``false alarms``
    Of the controls, changes the gate is documented to tolerate (reordering, a
    new field, a masked value, an empty page in contract mode), the share that
    made the case FAIL anyway.

The fault model is deliberately small and fixed, so the numbers mean the same
thing from one version to the next. A mutation is applied at one site only,
the first occurrence of each distinct path, because "one row in the page is
wrong" is how these defects actually ship.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from parity_gate import contracts
from parity_gate.differ import _child_path, is_masked
from parity_gate.evidence import ERROR, FAIL
from parity_gate.httpclient import Response
from parity_gate.runner import judge_contract, judge_differential, summarise
from parity_gate.schema import Schema, infer, parse_path, render_path
from parity_gate.suite import Case, Suite

DIFFERENTIAL = "differential"
CONTRACT = "contract"
MODES = (DIFFERENTIAL, CONTRACT)

DETECT = "detect"
TOLERATE = "tolerate"

CAUGHT = "caught"
MISSED = "missed"
TOLERATED = "tolerated"
FALSE_ALARM = "false_alarm"
TOOL_ERROR = "error"

ADDED_KEY = "__selftest_added__"
_INDEX = re.compile(r"\[\d+\]")

Step = tuple[str, Any]


@dataclass(frozen=True)
class Site:
    """One concrete place in a payload a fault can be injected."""

    steps: tuple[Step, ...]
    path: str
    value: Any

    @property
    def general(self) -> str:
        """The path with every index generalised, e.g. ``$.products[].price``.

        Only numeric subscripts are indices; a key that needed brackets is
        written quoted (``["a.b"]``) and is left alone.
        """
        return _INDEX.sub("[]", self.path)


@dataclass
class Sample:
    """A real response taken as the unmutated reference for one case."""

    case: Case
    status: int
    payload: Any


@dataclass
class Mutant:
    case_id: str
    mode: str
    operator: str
    site: str
    expectation: str
    outcome: str
    verdict: str
    findings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case_id,
            "mode": self.mode,
            "operator": self.operator,
            "site": self.site,
            "expectation": self.expectation,
            "outcome": self.outcome,
            "verdict": self.verdict,
            "findings": self.findings,
            "error": self.error,
        }


@dataclass
class Report:
    suite_name: str
    mutants: list[Mutant] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def of(self, mode: str, expectation: str) -> list[Mutant]:
        return [m for m in self.mutants if m.mode == mode and m.expectation == expectation]

    def detection(self, mode: str) -> tuple[int, int]:
        faults = self.of(mode, DETECT)
        return sum(1 for m in faults if m.outcome == CAUGHT), len(faults)

    def false_alarms(self, mode: str) -> tuple[int, int]:
        controls = self.of(mode, TOLERATE)
        return sum(1 for m in controls if m.outcome == FALSE_ALARM), len(controls)

    @property
    def errors(self) -> list[Mutant]:
        return [m for m in self.mutants if m.outcome == TOOL_ERROR]

    def to_dict(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for mode in sorted({m.mode for m in self.mutants}):
            caught, faults = self.detection(mode)
            alarms, controls = self.false_alarms(mode)
            summary[mode] = {
                "detected": caught,
                "faults": faults,
                "detection_rate": _rate(caught, faults),
                "false_alarms": alarms,
                "controls": controls,
                "false_alarm_rate": _rate(alarms, controls),
            }
        return {
            "suite": self.suite_name,
            "summary": summary,
            "errors": len(self.errors),
            "skipped_cases": self.skipped,
            "mutants": [m.to_dict() for m in self.mutants],
        }


# -- the fault model ----------------------------------------------------------


@dataclass(frozen=True)
class Operator:
    name: str
    description: str
    #: What the gate must do with the mutant, per mode.
    expect: dict[str, str]
    #: Yields (site label, mutated payload, mutated status) for one sample.
    apply: Callable[[Sample, Suite], Iterator[tuple[str, Any, int]]]


def _scalar_sites(sample: Sample, suite: Suite, *, masked: bool) -> Iterator[Site]:
    masks = suite.diff_options(sample.case).mask_paths
    for site in _first_per_path(_walk(sample.payload)):
        if isinstance(site.value, dict | list) or site.value is None or not site.steps:
            continue
        if _excluded(sample.case, site):
            continue
        if is_masked(site.path, masks) == masked:
            yield site


def _type_swap(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    for site in _scalar_sites(sample, suite, masked=False):
        value = site.value
        if isinstance(value, bool):
            swapped: Any = str(value).lower()
        elif isinstance(value, int | float):
            swapped = str(value)
        else:
            swapped = len(value)
        yield site.path, _replace(sample.payload, site.steps, swapped), sample.status


def _null_inject(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    for site in _scalar_sites(sample, suite, masked=False):
        yield site.path, _replace(sample.payload, site.steps, None), sample.status


def _perturb(value: Any, tolerance: float) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + max(1, int(tolerance * 2) + 1)
    if isinstance(value, float):
        return value + max(1.0, tolerance * 2)
    return f"{value}~"


def _value_change(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    tolerance = suite.diff_options(sample.case).float_tolerance
    for site in _scalar_sites(sample, suite, masked=False):
        mutated = _replace(sample.payload, site.steps, _perturb(site.value, tolerance))
        yield site.path, mutated, sample.status


def _masked_value_change(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    tolerance = suite.diff_options(sample.case).float_tolerance
    for site in _scalar_sites(sample, suite, masked=True):
        mutated = _replace(sample.payload, site.steps, _perturb(site.value, tolerance))
        yield site.path, mutated, sample.status


def _field_drop(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    shape = infer(sample.payload)
    for site in _first_per_path(_walk(sample.payload)):
        if not site.steps or site.steps[-1][0] != "k" or _excluded(sample.case, site):
            continue
        # Only fields the reference shows as always present: dropping one a
        # consumer already had to treat as optional is not a fault.
        if not shape.is_required(parse_path(site.general)):
            continue
        yield site.path, _delete(sample.payload, site.steps), sample.status


def _field_add(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    for site in _first_per_path(_walk(sample.payload)):
        if not isinstance(site.value, dict) or _excluded(sample.case, site):
            continue
        added = {**site.value, ADDED_KEY: 1}
        yield (
            _child_path(site.path, ADDED_KEY),
            _replace(sample.payload, site.steps, added),
            (sample.status),
        )


def _arrays(sample: Sample, minimum: int) -> Iterator[Site]:
    for site in _first_per_path(_walk(sample.payload)):
        if isinstance(site.value, list) and len(site.value) >= minimum:
            if not _excluded(sample.case, site):
                yield site


def _reorder(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    for site in _arrays(sample, 2):
        rendered = [json.dumps(item, sort_keys=True) for item in site.value]
        if len(set(rendered)) < 2:
            continue
        reordered = list(reversed(site.value))
        yield site.path, _replace(sample.payload, site.steps, reordered), sample.status


def _item_drop(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    for site in _arrays(sample, 2):
        shorter = site.value[:-1]
        yield site.path, _replace(sample.payload, site.steps, shorter), sample.status


def _collection_empty(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    for site in _arrays(sample, 1):
        yield site.path, _replace(sample.payload, site.steps, []), sample.status


def _status_change(sample: Sample, suite: Suite) -> Iterator[tuple[str, Any, int]]:
    changed = 404 if 200 <= sample.status < 300 else 200
    yield f"status {sample.status} -> {changed}", copy.deepcopy(sample.payload), changed


def operators(suite: Suite) -> list[Operator]:
    """The fixed fault model. Expectations follow the suite's own settings."""
    reorder = DETECT if not suite.ignore_array_order else TOLERATE
    return [
        Operator(
            "TYPE_SWAP",
            "a value changes JSON type",
            {DIFFERENTIAL: DETECT, CONTRACT: DETECT},
            _type_swap,
        ),
        Operator(
            "NULL_INJECT",
            "a value becomes null",
            {DIFFERENTIAL: DETECT, CONTRACT: DETECT},
            _null_inject,
        ),
        Operator(
            "FIELD_DROP",
            "an always-present field is missing",
            {DIFFERENTIAL: DETECT, CONTRACT: DETECT},
            _field_drop,
        ),
        Operator(
            "STATUS_CHANGE",
            "the status code changes",
            {DIFFERENTIAL: DETECT, CONTRACT: DETECT},
            _status_change,
        ),
        Operator(
            "VALUE_CHANGE",
            "a value changes, type intact",
            {DIFFERENTIAL: DETECT, CONTRACT: TOLERATE},
            _value_change,
        ),
        Operator(
            "ITEM_DROP",
            "a collection loses an item",
            {DIFFERENTIAL: DETECT, CONTRACT: TOLERATE},
            _item_drop,
        ),
        Operator(
            "COLLECTION_EMPTY",
            "a collection comes back empty",
            {DIFFERENTIAL: DETECT, CONTRACT: TOLERATE},
            _collection_empty,
        ),
        Operator(
            "REORDER",
            "a collection comes back in another order",
            {DIFFERENTIAL: reorder, CONTRACT: TOLERATE},
            _reorder,
        ),
        Operator(
            "FIELD_ADD",
            "a new field appears",
            {DIFFERENTIAL: TOLERATE, CONTRACT: TOLERATE},
            _field_add,
        ),
        Operator(
            "MASKED_VALUE_CHANGE",
            "a value under a mask path changes",
            {DIFFERENTIAL: TOLERATE, CONTRACT: TOLERATE},
            _masked_value_change,
        ),
    ]


# -- running it ---------------------------------------------------------------


def run(
    suite: Suite,
    samples: list[Sample],
    *,
    modes: tuple[str, ...] = MODES,
    max_sites: int = 25,
) -> Report:
    """Inject the fault model into every sample and judge each mutant.

    Waivers are set aside: they are the team's accepted exceptions, and a
    fault the gate detects but a waiver accepts is still a fault detected.
    """
    suite = dataclasses.replace(suite, waivers=[])
    report = Report(suite_name=suite.name)
    for sample in samples:
        for operator in operators(suite):
            for index, (site, payload, status) in enumerate(operator.apply(sample, suite)):
                if index >= max_sites:
                    break
                for mode in modes:
                    expectation = operator.expect[mode]
                    if expectation == TOLERATE and _removes_required(sample, payload):
                        continue  # the case's own assertion forbids this change
                    report.mutants.append(
                        _judge(suite, sample, mode, operator, expectation, site, payload, status)
                    )
    return report


def _judge(
    suite: Suite,
    sample: Sample,
    mode: str,
    operator: Operator,
    expectation: str,
    site: str,
    payload: Any,
    status: int,
) -> Mutant:
    case = sample.case
    repeats = max(1, suite.repeats_for(case))
    options = suite.diff_options(case)
    mutant = Mutant(
        case_id=case.id,
        mode=mode,
        operator=operator.name,
        site=site,
        expectation=expectation,
        outcome=TOOL_ERROR,
        verdict=ERROR,
    )
    try:
        candidate = summarise(_responses(case, status, payload, repeats), options)
        if mode == DIFFERENTIAL:
            baseline = summarise(_responses(case, sample.status, sample.payload, repeats), options)
            record = judge_differential(suite, case, baseline, candidate)
        else:
            record = judge_contract(suite, case, _contract_for(suite, sample, repeats), candidate)
    except Exception as exc:
        mutant.error = f"{type(exc).__name__}: {exc}"
        return mutant

    mutant.verdict = record.verdict
    mutant.findings = _findings(record)
    if record.verdict == ERROR:
        mutant.error = record.error
        return mutant
    failed = record.verdict == FAIL
    if expectation == DETECT:
        mutant.outcome = CAUGHT if failed else MISSED
    else:
        mutant.outcome = FALSE_ALARM if failed else TOLERATED
    return mutant


def _contract_for(suite: Suite, sample: Sample, repeats: int) -> contracts.Contract:
    """A contract recorded from the reference, through the same file round trip."""
    shape = infer(*[copy.deepcopy(sample.payload) for _ in range(repeats)])
    recorded = contracts.RecordedCase(
        schema=Schema.from_portable(shape.to_portable()),
        statuses=[sample.status],
        stability="STABLE",
    )
    return contracts.Contract(
        suite_name=suite.name,
        source_url="selftest reference sample",
        recorded_at=contracts.now(),
        cases={sample.case.id: recorded},
    )


def _responses(case: Case, status: int, payload: Any, repeats: int) -> list[Response]:
    text = json.dumps(payload)
    return [
        Response(
            url=f"selftest:{case.path}",
            method=case.method,
            status=status,
            body_text=text,
            json_body=json.loads(text),
            attempts=1,
        )
        for _ in range(repeats)
    ]


def _findings(record: Any) -> list[str]:
    found = [f"{c.get('rule') or '-'} {c['name']}" for c in record.checks if not c["passed"]]
    found += [
        f"{d.get('rule') or '-'} {d['kind']} {d['severity']} {d['path']}" for d in record.drifts
    ]
    found += [f"{d.get('rule') or '-'} {d['kind']} {d['path']}" for d in record.differences]
    return found


# -- payload plumbing ---------------------------------------------------------


def _walk(value: Any, steps: tuple[Step, ...] = (), path: str = "$") -> Iterator[Site]:
    yield Site(steps=steps, path=path, value=value)
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, (*steps, ("k", key)), _child_path(path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, (*steps, ("i", index)), f"{path}[{index}]")


def _first_per_path(sites: Iterator[Site]) -> Iterator[Site]:
    seen: set[str] = set()
    for site in sites:
        if site.general not in seen:
            seen.add(site.general)
            yield site


def _excluded(case: Case, site: Site) -> bool:
    """GraphQL's ``errors`` array has its own check; mutating it tests that check."""
    return case.is_graphql and site.path.startswith("$.errors")


def _replace(payload: Any, steps: tuple[Step, ...], value: Any) -> Any:
    if not steps:
        return copy.deepcopy(value)
    root = copy.deepcopy(payload)
    parent = root
    for _, key in steps[:-1]:
        parent = parent[key]
    parent[steps[-1][1]] = value
    return root


def _delete(payload: Any, steps: tuple[Step, ...]) -> Any:
    root = copy.deepcopy(payload)
    parent = root
    for _, key in steps[:-1]:
        parent = parent[key]
    del parent[steps[-1][1]]
    return root


def _removes_required(sample: Sample, mutated: Any) -> bool:
    if not sample.case.required_fields:
        return False
    before = {render_path(p) for p in infer(sample.payload).fields}
    after = {render_path(p) for p in infer(mutated).fields}
    return any(f in before and f not in after for f in sample.case.required_fields)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None
