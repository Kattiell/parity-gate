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

from parity_gate.rules import DRIFT, rule_id

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


def parse_path(display: str) -> Path:
    """Inverse of :func:`render_path`.

    A recorded contract gets committed and reviewed in pull requests, so it
    stores the readable spelling (``$.products[].price``) rather than nested
    arrays of segments. That only works if the spelling round-trips exactly,
    which is what this does and what ``test_schema`` pins.
    """
    if not display.startswith("$"):
        raise ValueError(f"path must start with '$': {display!r}")

    segments: list[Segment] = []
    index = 1
    decoder = json.JSONDecoder()

    while index < len(display):
        char = display[index]
        if char == ".":
            end = index + 1
            while end < len(display) and display[end] not in ".[":
                end += 1
            segments.append(("k", display[index + 1 : end]))
            index = end
        elif display.startswith("[]", index):
            segments.append(("i",))
            index += 2
        elif char == "[":
            key, end = decoder.raw_decode(display, index + 1)
            if not isinstance(key, str) or end >= len(display) or display[end] != "]":
                raise ValueError(f"malformed bracketed key in {display!r} at {index}")
            segments.append(("k", key))
            index = end + 1
        else:
            raise ValueError(f"unexpected character {char!r} in {display!r} at {index}")

    return tuple(segments)


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
    #: Set only when the schema was loaded from a recorded contract. Whether a
    #: field was always present was decided at record time, from counts that no
    #: longer exist, so it is carried rather than recomputed.
    recorded_required: dict[Path, bool] | None = None

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
        if self.recorded_required is not None:
            return self.recorded_required.get(path, False)
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
        """Human-facing form, keyed by rendered path. Used in evidence files."""
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

    def to_portable(self) -> dict[str, Any]:
        """Lossless form that survives a round trip through a file.

        ``to_dict`` renders paths for people and cannot be parsed back without
        ambiguity, so a recorded contract keeps the raw segments alongside the
        readable spelling.
        """
        return {
            "samples": self.samples,
            "fingerprint": self.fingerprint(),
            "fields": [
                {
                    "path": render_path(path),
                    "types": sorted(self.fields[path].type_names),
                    "required": self.is_required(path),
                }
                for path in sorted(self.fields, key=render_path)
            ],
        }

    @classmethod
    def from_portable(cls, data: dict[str, Any]) -> Schema:
        """Rebuild a schema previously written by :meth:`to_portable`."""
        schema = cls(samples=int(data.get("samples", 0)))
        schema.recorded_required = {}
        for entry in data.get("fields", []):
            path: Path = parse_path(entry["path"])
            field_entry = Field(path=path)
            for name in entry["types"]:
                field_entry.types[name] = 1
            field_entry.present = 1
            schema.fields[path] = field_entry
            schema.recorded_required[path] = bool(entry.get("required", False))
        return schema


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
            "rule": rule_id(DRIFT, self.kind),
            "path": self.path,
            "kind": self.kind,
            "severity": self.severity.value,
            "detail": self.detail,
        }


# Widenings a tolerant consumer usually survives, so they are RISKY rather than
# BREAKING: a parser that accepted 3 normally accepts 3.5.
_SAFE_WIDENINGS = frozenset({frozenset({"integer", "number"})})


def unobserved_because_empty(schema: Schema, path: Path) -> bool:
    """True when nothing could be learned about ``path`` from these samples.

    A collection that came back empty says nothing about the shape of its
    items. Treating "the page had no rows today" as "the fields were removed"
    is the single most effective way to make a gate get switched off: search,
    filters and pagination past the last page all produce empty collections on
    a perfectly healthy service.

    So the subtree under an empty array is *unobserved*, which is a different
    fact from *absent*, and is reported as such rather than as a regression.
    """
    for cut in range(1, len(path) + 1):
        prefix = path[:cut]
        if prefix[-1][0] != "i" or prefix in schema.fields:
            continue
        container = schema.fields.get(prefix[:-1])
        if container is not None and container.types.get("array", 0) > 0:
            return True
    return False


def compare(baseline: Schema, candidate: Schema) -> tuple[list[Drift], list[str]]:
    """Classify every structural difference from ``baseline`` to ``candidate``.

    The direction matters: ``baseline`` is the contract consumers were written
    against, ``candidate`` is what the rewrite now returns.

    Returns the findings and, separately, the paths that could not be checked
    at all because a collection was empty on one side. Those are not findings,
    but they are not silence either, because a gate that quietly stops checking
    a subtree gives false confidence.
    """
    drifts: list[Drift] = []
    unchecked: list[str] = []
    paths = sorted(set(baseline.fields) | set(candidate.fields), key=render_path)

    for path in paths:
        rendered = render_path(path)
        in_base = path in baseline.fields
        in_cand = path in candidate.fields

        if in_base and not in_cand:
            if unobserved_because_empty(candidate, path):
                unchecked.append(rendered)
                continue
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
            if unobserved_because_empty(baseline, path):
                # The other side's collection was empty when it was sampled, so
                # this is the first sight of the field, not an addition to it.
                unchecked.append(rendered)
                continue
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
    return drifts, unchecked


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
        # A new type outside the numeric family is one no existing parser of
        # this field has seen. It usually arrives in *one* item of a page (one
        # product's price serialised as a string), which is exactly why it
        # shows up as a union rather than a replacement, and it breaks that
        # consumer as surely as a full TYPE_CHANGED would. The exception is a
        # field the baseline only ever returned as null: its type was never
        # observed, so a first real value is worth a look, not a failed build.
        never_typed = base_types == {"null"}
        found.append(
            Drift(
                path=rendered,
                kind="TYPE_WIDENED",
                severity=Severity.RISKY if never_typed else Severity.BREAKING,
                detail=(
                    f"baseline only ever returned null here, so its type was never observed "
                    f"({summary})"
                    if never_typed
                    else f"candidate returns a type consumers of this field never received "
                    f"({summary})"
                ),
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
