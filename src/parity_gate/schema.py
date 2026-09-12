"""Schema inference and contract-drift classification.

Nobody writes the JSON Schema for the endpoint they are about to change, so
this module derives the shape of a response from the responses themselves and
then answers the only question that matters during a migration: is the new
shape something the old consumers can still read?

Paths are kept as tuples internally (``("k", "products"), ("i",), ("k", "id")``)
so that a field literally named ``a.b`` cannot be confused with a nested one,
and are rendered as ``$.products[].id`` only for human output.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Segment = tuple[str, ...]
Path = tuple[Segment, ...]


class Severity(StrEnum):
    """How much a single drift finding should worry the person reading it."""

    BREAKING = "breaking"
    RISKY = "risky"
    ADDITIVE = "additive"

    @property
    def rank(self) -> int:
        return {"breaking": 3, "risky": 2, "additive": 1}[self.value]


def type_of(value: Any) -> str:
    """JSON type name for ``value``; ``bool`` is checked before ``int``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def render_path(path: Path) -> str:
    """Render an internal path tuple as ``$.products[].id``."""
    out = "$"
    for segment in path:
        if segment[0] == "i":
            out += "[]"
        else:
            key = segment[1]
            if key and key.replace("_", "").replace("-", "").isalnum() and not key[:1].isdigit():
                out += f".{key}"
            else:
                out += f"[{json.dumps(key)}]"
    return out


@dataclass
class Field:
    """What was observed at one path across one or more sample payloads."""

    path: Path
    types: Counter[str] = field(default_factory=Counter)
    present: int = 0

    @property
    def type_names(self) -> frozenset[str]:
        return frozenset(self.types)

    @property
    def nullable(self) -> bool:
        return "null" in self.types


@dataclass
class Schema:
    """An inferred contract: every reachable path and the types seen there."""

    fields: dict[Path, Field] = field(default_factory=dict)
    samples: int = 0

    def observe(self, value: Any) -> None:
        """Fold one more payload into the schema."""
        self.samples += 1
        self._walk(value, ())

    def _walk(self, value: Any, path: Path) -> None:
        entry = self.fields.get(path)
        if entry is None:
            entry = self.fields[path] = Field(path=path)
        entry.types[type_of(value)] += 1
        entry.present += 1
        if isinstance(value, dict):
            for key, item in value.items():
                self._walk(item, (*path, ("k", str(key))))
        elif isinstance(value, list):
            for item in value:
                self._walk(item, (*path, ("i",)))

    def is_required(self, path: Path) -> bool:
        """True when the field appeared in every parent object that could hold it.

        Array elements are never "required": an empty array is a legitimate
        response, and treating a missing element as a removed field is the
        classic source of false alarms in contract diffs.
        """
        if not path or path[-1][0] == "i":
            return True
        parent = self.fields.get(path[:-1])
        entry = self.fields.get(path)
        if parent is None or entry is None:
            return False
        parent_objects = parent.types.get("object", 0)
        return parent_objects > 0 and entry.present == parent_objects

    def fingerprint(self) -> str:
        """Stable hash of the shape only, ignoring how often each path was seen.

        Two runs of the same endpoint that return different data but the same
        structure share a fingerprint, which is what separates "the payload is
        noisy" from "the payload changed shape" during flakiness triage.
        """
        digest = hashlib.sha256()
        for path in sorted(self.fields, key=render_path):
            entry = self.fields[path]
            digest.update(render_path(path).encode())
            digest.update(b"\x00")
            digest.update(",".join(sorted(entry.type_names)).encode())
            digest.update(b"\x00" if self.is_required(path) else b"\x01")
        return digest.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "fingerprint": self.fingerprint(),
            "fields": {
                render_path(path): {
                    "types": sorted(entry.type_names),
                    "required": self.is_required(path),
                    "nullable": entry.nullable,
                }
                for path, entry in sorted(self.fields.items(), key=lambda kv: render_path(kv[0]))
            },
        }


def infer(*payloads: Any) -> Schema:
    """Build a :class:`Schema` from one or more sample payloads."""
    schema = Schema()
    for payload in payloads:
        schema.observe(payload)
    return schema


@dataclass(frozen=True)
class Drift:
    """One difference between a baseline contract and a candidate contract."""

    path: str
    kind: str
    severity: Severity
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "severity": self.severity.value,
            "detail": self.detail,
        }


# Widenings a tolerant consumer usually survives, so they are RISKY rather than
# BREAKING: a parser that accepted 3 normally accepts 3.5.
_SAFE_WIDENINGS = frozenset({frozenset({"integer", "number"})})


def compare(baseline: Schema, candidate: Schema) -> list[Drift]:
    """Classify every structural difference from ``baseline`` to ``candidate``.

    The direction matters: ``baseline`` is the contract consumers were written
    against, ``candidate`` is what the rewrite now returns.
    """
    drifts: list[Drift] = []
    paths = sorted(set(baseline.fields) | set(candidate.fields), key=render_path)

    for path in paths:
        rendered = render_path(path)
        in_base = path in baseline.fields
        in_cand = path in candidate.fields

        if in_base and not in_cand:
            required = baseline.is_required(path)
            drifts.append(
                Drift(
                    path=rendered,
                    kind="FIELD_REMOVED",
                    severity=Severity.BREAKING if required else Severity.RISKY,
                    detail=(
                        "present in baseline but absent from candidate"
                        + (" (was always present)" if required else " (was optional)")
                    ),
                )
            )
            continue

        if in_cand and not in_base:
            drifts.append(
                Drift(
                    path=rendered,
                    kind="FIELD_ADDED",
                    severity=Severity.ADDITIVE,
                    detail="new in candidate; harmless for consumers that ignore unknown fields",
                )
            )
            continue

        drifts.extend(_compare_field(baseline, candidate, path, rendered))

    drifts.sort(key=lambda d: (-d.severity.rank, d.path))
    return drifts


def _compare_field(baseline: Schema, candidate: Schema, path: Path, rendered: str) -> list[Drift]:
    found: list[Drift] = []
    base_types = baseline.fields[path].type_names
    cand_types = candidate.fields[path].type_names
    base_required = baseline.is_required(path)
    cand_required = candidate.is_required(path)

    if base_required and not cand_required:
        found.append(
            Drift(
                path=rendered,
                kind="FIELD_NOW_OPTIONAL",
                severity=Severity.BREAKING,
                detail="always present in baseline, sometimes missing in candidate",
            )
        )
    elif cand_required and not base_required:
        found.append(
            Drift(
                path=rendered,
                kind="FIELD_NOW_ALWAYS_PRESENT",
                severity=Severity.ADDITIVE,
                detail="optional in baseline, always present in candidate",
            )
        )

    if base_types == cand_types:
        return found

    added = cand_types - base_types
    removed = base_types - cand_types
    summary = f"{sorted(base_types)} -> {sorted(cand_types)}"

    if added == {"null"} and not removed:
        found.append(
            Drift(
                path=rendered,
                kind="NULLABLE_ADDED",
                severity=Severity.BREAKING,
                detail=f"candidate can return null where baseline never did ({summary})",
            )
        )
    elif (base_types | cand_types) in _SAFE_WIDENINGS:
        # Checked before the added/removed split: `integer -> number` replaces
        # the type outright, but a consumer that read 3 will read 3.5.
        found.append(
            Drift(
                path=rendered,
                # `number` is the wider of the pair, so which side holds it
                # decides the direction.
                kind="TYPE_WIDENED" if "number" in cand_types else "TYPE_NARROWED",
                severity=Severity.RISKY,
                detail=f"numeric type changed within the same family ({summary})",
            )
        )
    elif not removed:
        found.append(
            Drift(
                path=rendered,
                kind="TYPE_WIDENED",
                severity=Severity.RISKY,
                detail=f"candidate returns extra types ({summary})",
            )
        )
    elif not added:
        found.append(
            Drift(
                path=rendered,
                kind="TYPE_NARROWED",
                severity=Severity.RISKY,
                detail=f"candidate stopped returning some types ({summary})",
            )
        )
    else:
        found.append(
            Drift(
                path=rendered,
                kind="TYPE_CHANGED",
                severity=Severity.BREAKING,
                detail=f"incompatible type change ({summary})",
            )
        )
    return found


def worst(drifts: list[Drift]) -> Severity | None:
    """Highest severity in ``drifts``, or ``None`` when the contract held."""
    if not drifts:
        return None
    return max((d.severity for d in drifts), key=lambda s: s.rank)
