"""Flakiness triage: separate "the test is broken" from "the payload is noisy".

Timing and test data cause the large majority of flaky failures, and both show
up the same way in a report, as a diff that was green a minute ago. Calling the
same endpoint N times and classifying what varied tells you which one you have:

``STABLE``
    Same status, same shape, same bytes. A later diff is a real difference.
``VOLATILE_BODY``
    Same status and shape, different values. Usually a timestamp or a generated
    id, so the tool proposes the exact mask paths to add to the suite instead of
    leaving you to find them.
``FLAKY_SHAPE``
    The structure itself changed between identical calls. Often a partially
    populated cache or a field only serialised when non-empty.
``FLAKY_STATUS``
    The status code changed between identical calls. This is the one to fix
    before trusting any other result in the report.

A run that starts from a flaky endpoint produces a diff nobody can act on, so
stability is measured first and reported next to every finding.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from parity_gate.differ import DiffOptions, canonical, diff
from parity_gate.schema import infer

STABLE = "STABLE"
VOLATILE_BODY = "VOLATILE_BODY"
FLAKY_SHAPE = "FLAKY_SHAPE"
FLAKY_STATUS = "FLAKY_STATUS"

_INDEX = re.compile(r"\[\d+\]")


@dataclass
class Stability:
    """The outcome of calling one endpoint several times in a row."""

    repeats: int
    verdict: str
    statuses: list[int | None] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    volatile_paths: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def trustworthy(self) -> bool:
        """Whether a parity diff on this endpoint can be believed."""
        return self.verdict in {STABLE, VOLATILE_BODY}

    @property
    def p50_ms(self) -> float:
        return round(statistics.median(self.latencies_ms), 2) if self.latencies_ms else 0.0

    @property
    def max_ms(self) -> float:
        return round(max(self.latencies_ms), 2) if self.latencies_ms else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repeats": self.repeats,
            "verdict": self.verdict,
            "statuses": self.statuses,
            "p50_ms": self.p50_ms,
            "max_ms": self.max_ms,
            "volatile_paths": self.volatile_paths,
            "detail": self.detail,
        }


def classify(
    statuses: list[int | None],
    payloads: list[Any],
    latencies_ms: list[float],
    options: DiffOptions | None = None,
) -> Stability:
    """Classify repeated observations of the same request."""
    opts = options or DiffOptions()
    repeats = len(statuses)
    result = Stability(
        repeats=repeats, verdict=STABLE, statuses=list(statuses), latencies_ms=list(latencies_ms)
    )

    if repeats < 2:
        result.detail = "single sample; stability not measured"
        return result

    distinct_statuses = sorted({s for s in statuses if s is not None}) or []
    if len({*statuses}) > 1:
        result.verdict = FLAKY_STATUS
        result.detail = f"status varied across identical calls: {distinct_statuses or statuses}"
        return result

    shapes = {infer(payload).fingerprint() for payload in payloads}
    if len(shapes) > 1:
        result.verdict = FLAKY_SHAPE
        result.volatile_paths = _suggest_masks(payloads, opts)
        result.detail = f"{len(shapes)} different response shapes across {repeats} identical calls"
        return result

    bodies = {canonical(payload, opts.mask_paths) for payload in payloads}
    if len(bodies) > 1:
        result.verdict = VOLATILE_BODY
        result.volatile_paths = _suggest_masks(payloads, opts)
        result.detail = (
            "values changed between identical calls; "
            "add the suggested paths to policy.mask_paths to stop diffing noise"
        )
        return result

    result.detail = "identical status, shape and body across all samples"
    return result


def _suggest_masks(payloads: list[Any], options: DiffOptions) -> list[str]:
    """Paths whose value moved between identical calls, generalised over indices.

    ``$.items[0].updatedAt`` and ``$.items[1].updatedAt`` collapse to
    ``$.items[].updatedAt`` so the suggestion can be pasted straight into a
    suite and cover the whole array.
    """
    found: set[str] = set()
    reference = payloads[0]
    for other in payloads[1:]:
        for difference in diff(reference, other, options):
            found.add(_INDEX.sub("[]", difference.path))
    return sorted(found)
