"""Flakiness across runs, read from the evidence bundles already on disk.

Stability sampling calls an endpoint a few times in a row. That finds frequent
flakiness and structurally cannot see a slower period: a cache that expires
hourly, a replica that rotates, a nightly batch that locks a table. Rerunning
something immediately is the weakest way to find flakiness; running it again
later, under other conditions, is what exposes the rest.

The later repetitions already exist. Every CI run writes a bundle, and each
bundle records every case's verdict, the statuses it saw, its response shape
and, since this version, the revision under test. Reading them in order
answers the question sampling cannot:

``INTERMITTENT``
    The case changed outcome and then changed back, or two runs of the **same
    revision** disagreed. Nothing about the code explains that; it is flaky.
``REGRESSED``
    It passed, then failed, and kept failing. A change, not noise.
``RECOVERED``
    It failed, then passed, and kept passing.
``STEADY``
    The same outcome every time it ran.

Only bundles that pass ``verify`` are read. A history assembled from evidence
that was edited is not a history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from parity_gate import evidence

STEADY = "STEADY"
INTERMITTENT = "INTERMITTENT"
REGRESSED = "REGRESSED"
RECOVERED = "RECOVERED"

_FAILING = {evidence.FAIL, evidence.ERROR}


@dataclass
class Observation:
    run_id: str
    started_at: str
    revision: str | None
    verdict: str
    statuses: list[int]
    fingerprint: str | None

    @property
    def failing(self) -> bool:
        return self.verdict in _FAILING


@dataclass
class CaseHistory:
    suite: str
    case_id: str
    observations: list[Observation] = field(default_factory=list)

    @property
    def outcomes(self) -> list[bool]:
        return [o.failing for o in self.observations]

    @property
    def flips(self) -> int:
        outcomes = self.outcomes
        return sum(1 for a, b in pairwise(outcomes) if a != b)

    @property
    def disagreeing_revisions(self) -> dict[str, list[str]]:
        """Revisions under which this case produced more than one outcome."""
        seen: dict[str, set[bool]] = {}
        verdicts: dict[str, list[str]] = {}
        for o in self.observations:
            if not o.revision:
                continue
            seen.setdefault(o.revision, set()).add(o.failing)
            verdicts.setdefault(o.revision, []).append(o.verdict)
        return {
            rev: sorted(set(verdicts[rev])) for rev, outcomes in seen.items() if len(outcomes) > 1
        }

    @property
    def classification(self) -> str:
        if self.disagreeing_revisions or self.flips >= 2:
            return INTERMITTENT
        if self.flips == 1:
            return REGRESSED if self.outcomes[-1] else RECOVERED
        return STEADY

    @property
    def statuses_seen(self) -> list[int]:
        return sorted({s for o in self.observations for s in o.statuses})

    @property
    def shapes_seen(self) -> int:
        return len({o.fingerprint for o in self.observations if o.fingerprint})

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "case": self.case_id,
            "classification": self.classification,
            "runs": len(self.observations),
            "flips": self.flips,
            "flip_rate": round(self.flips / (len(self.observations) - 1), 4)
            if len(self.observations) > 1
            else 0.0,
            "disagreeing_revisions": self.disagreeing_revisions,
            "statuses_seen": self.statuses_seen,
            "shapes_seen": self.shapes_seen,
            "timeline": [
                {"run": o.run_id, "at": o.started_at, "revision": o.revision, "verdict": o.verdict}
                for o in self.observations
            ],
        }


@dataclass
class History:
    bundles_read: int = 0
    rejected: dict[str, list[str]] = field(default_factory=dict)
    cases: list[CaseHistory] = field(default_factory=list)

    def by(self, classification: str) -> list[CaseHistory]:
        return [c for c in self.cases if c.classification == classification]

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundles_read": self.bundles_read,
            "rejected_bundles": self.rejected,
            "cases": [c.to_dict() for c in self.cases],
        }


def read(root: Path, *, last: int | None = None) -> History:
    """Every verified bundle under ``root``, oldest first, grouped by suite and case."""
    history = History()
    dated: list[tuple[str, float, dict[str, Any]]] = []
    for bundle in sorted(d for d in root.iterdir() if (d / "manifest.json").is_file()):
        problems = evidence.verify(bundle)
        if problems:
            history.rejected[bundle.name] = problems
            continue
        run_file = bundle / "run.json"
        run = json.loads(run_file.read_text(encoding="utf-8"))
        # Timestamps are to the second; the file's own mtime breaks the tie
        # between two runs that started in the same one.
        dated.append((str(run.get("started_at") or ""), run_file.stat().st_mtime, run))

    runs = [run for _, _, run in sorted(dated, key=lambda item: (item[0], item[1]))]
    if last:
        runs = runs[-last:]
    history.bundles_read = len(runs)

    grouped: dict[tuple[str, str], CaseHistory] = {}
    for run in runs:
        suite = str((run.get("suite") or {}).get("name") or "")
        revision = (run.get("context") or {}).get("revision")
        for record in run.get("records", []):
            case_id = str((record.get("case") or {}).get("id"))
            entry = grouped.setdefault((suite, case_id), CaseHistory(suite, case_id))
            candidate = (record.get("stability") or {}).get("candidate") or {}
            schema = (record.get("schema") or {}).get("candidate") or {}
            entry.observations.append(
                Observation(
                    run_id=str(run.get("run_id")),
                    started_at=str(run.get("started_at")),
                    revision=revision,
                    verdict=str(record.get("verdict")),
                    statuses=[s for s in candidate.get("statuses") or [] if isinstance(s, int)],
                    fingerprint=schema.get("fingerprint"),
                )
            )

    order = {INTERMITTENT: 0, REGRESSED: 1, RECOVERED: 2, STEADY: 3}
    history.cases = sorted(
        grouped.values(), key=lambda c: (order[c.classification], c.suite, c.case_id)
    )
    return history
