"""Value-level parity diffing between a baseline response and a candidate one.

Schema drift catches "the shape changed". This module catches the other half:
the shape is identical and the *values* silently differ, which is how a rewrite
ships a wrong tax rate, a truncated list or an off-by-one page.

Two design choices keep the signal usable:

* **Volatile paths are masked, not compared.** Timestamps and request ids differ
  on every call by definition. Diffing them buries the one real finding under a
  hundred fake ones, which is how teams learn to ignore the report.
* **Collections are matched by identity, not by position.** Given a key, item
  ``id=7`` is compared against item ``id=7`` on the other side. Without this,
  a reordered page of four products reports a difference on every field of
  every item and the one genuine regression is invisible.
* **Reordering is one finding, not N.** When two arrays hold the same items in a
  different order, that is a single ordering difference.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from parity_gate.schema import type_of

MASKED = "<masked>"

#: Any array subscript, so that a mask written as ``$.items[].updatedAt``
#: covers ``$.items[0].updatedAt`` and ``$.items[id=7].updatedAt`` alike.
_SUBSCRIPT = re.compile(r"\[[^\]]*\]")


@dataclass(frozen=True)
class Difference:
    """One concrete divergence between the two responses."""

    path: str
    kind: str
    baseline: Any
    candidate: Any
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "baseline": _truncate(self.baseline),
            "candidate": _truncate(self.candidate),
            "detail": self.detail,
        }


@dataclass
class DiffOptions:
    """How strict the comparison should be for one case."""

    #: Glob patterns over rendered paths, e.g. ``$.meta.requestId`` or
    #: ``$.products[].updatedAt``. Matching values are never compared.
    mask_paths: list[str] = field(default_factory=list)
    #: Array path pattern -> the field that identifies an item, e.g.
    #: ``{"$.products": "id"}``. Matched items are diffed against their
    #: counterpart instead of against whatever sits at the same index.
    array_keys: dict[str, str] = field(default_factory=dict)
    #: Treat arrays as unordered: same items in a different order is reported
    #: once as ``ORDER_ONLY`` instead of a difference per index.
    ignore_array_order: bool = True
    #: Absolute tolerance for float comparison. Money should stay at 0.
    float_tolerance: float = 0.0
    #: Stop after this many findings per case so one structural change cannot
    #: produce a ten-thousand-line report.
    max_differences: int = 200


def is_masked(path: str, patterns: list[str]) -> bool:
    """True when ``path`` matches any mask pattern (case-sensitive globs).

    The path is tested both as written and with every array subscript reduced
    to ``[]``, so one mask covers every item of a collection.
    """
    if not patterns:
        return False
    generalised = _SUBSCRIPT.sub("[]", path)
    return any(
        fnmatchcase(path, pattern) or fnmatchcase(generalised, pattern) for pattern in patterns
    )


def _array_key_for(path: str, array_keys: dict[str, str]) -> str | None:
    generalised = _SUBSCRIPT.sub("[]", path)
    for pattern, key in array_keys.items():
        if fnmatchcase(path, pattern) or fnmatchcase(generalised, pattern):
            return key
    return None


def diff(baseline: Any, candidate: Any, options: DiffOptions | None = None) -> list[Difference]:
    """Compare two decoded JSON values and return every meaningful difference."""
    opts = options or DiffOptions()
    found: list[Difference] = []
    _diff(baseline, candidate, "$", opts, found)
    return found[: opts.max_differences]


def _diff(base: Any, cand: Any, path: str, opts: DiffOptions, out: list[Difference]) -> None:
    if len(out) >= opts.max_differences:
        return
    if is_masked(path, opts.mask_paths):
        return

    base_type, cand_type = type_of(base), type_of(cand)
    if base_type != cand_type:
        if _numeric_pair(base_type, cand_type) and _close(base, cand, opts.float_tolerance):
            return
        out.append(
            Difference(
                path=path,
                kind="TYPE",
                baseline=base,
                candidate=cand,
                detail=f"{base_type} -> {cand_type}",
            )
        )
        return

    if base_type == "object":
        _diff_object(base, cand, path, opts, out)
    elif base_type == "array":
        _diff_array(base, cand, path, opts, out)
    elif base_type == "number" and not _close(base, cand, opts.float_tolerance):
        out.append(Difference(path=path, kind="VALUE", baseline=base, candidate=cand))
    elif base_type != "number" and base != cand:
        out.append(Difference(path=path, kind="VALUE", baseline=base, candidate=cand))


def _diff_object(
    base: dict[str, Any], cand: dict[str, Any], path: str, opts: DiffOptions, out: list[Difference]
) -> None:
    for key in base:
        child = _child_path(path, key)
        if is_masked(child, opts.mask_paths):
            continue
        if key not in cand:
            out.append(
                Difference(
                    path=child,
                    kind="MISSING_IN_CANDIDATE",
                    baseline=base[key],
                    candidate=None,
                    detail="key returned by baseline is absent from candidate",
                )
            )
        else:
            _diff(base[key], cand[key], child, opts, out)
    for key in cand:
        if key in base:
            continue
        child = _child_path(path, key)
        if is_masked(child, opts.mask_paths):
            continue
        out.append(
            Difference(
                path=child,
                kind="EXTRA_IN_CANDIDATE",
                baseline=None,
                candidate=cand[key],
                detail="key returned by candidate is absent from baseline",
            )
        )


def _diff_array(
    base: list[Any], cand: list[Any], path: str, opts: DiffOptions, out: list[Difference]
) -> None:
    key = _array_key_for(path, opts.array_keys)
    if key is not None:
        if _identifiable(base, key) and _identifiable(cand, key):
            _diff_keyed_array(base, cand, path, key, opts, out)
            return
        # Falling back to positional silently is how a reader ends up blaming
        # the API for noise the configuration caused.
        out.append(
            Difference(
                path=path,
                kind="KEY_MATCH_UNAVAILABLE",
                baseline=f"key {key!r} requested",
                candidate="compared by position instead",
                detail=(
                    f"items are not uniquely identified by {key!r} on both sides, so this "
                    "collection was compared by position; differences below may be ordering "
                    "artefacts rather than regressions"
                ),
            )
        )

    if len(base) != len(cand):
        out.append(
            Difference(
                path=path,
                kind="LENGTH",
                baseline=len(base),
                candidate=len(cand),
                detail=f"{len(base)} item(s) -> {len(cand)} item(s)",
            )
        )

    if opts.ignore_array_order and base and len(base) == len(cand):
        base_keys = sorted(canonical(item, opts.mask_paths, path + "[]") for item in base)
        cand_keys = sorted(canonical(item, opts.mask_paths, path + "[]") for item in cand)
        if base_keys == cand_keys:
            positional = [canonical(i, opts.mask_paths, path + "[]") for i in base] != [
                canonical(i, opts.mask_paths, path + "[]") for i in cand
            ]
            if positional:
                out.append(
                    Difference(
                        path=path,
                        kind="ORDER_ONLY",
                        baseline="same items",
                        candidate="different order",
                        detail=(
                            "identical items in a different order; assert on the sort key "
                            "if order is part of the contract"
                        ),
                    )
                )
            return

    for index, (base_item, cand_item) in enumerate(zip(base, cand, strict=False)):
        _diff(base_item, cand_item, f"{path}[{index}]", opts, out)


def _identifiable(items: list[Any], key: str) -> bool:
    """True when every item is an object carrying a unique scalar ``key``."""
    seen: set[Any] = set()
    for item in items:
        if not isinstance(item, dict) or key not in item:
            return False
        value = item[key]
        if isinstance(value, (dict, list)) or value in seen:
            return False
        seen.add(value)
    return True


def _diff_keyed_array(
    base: list[Any],
    cand: list[Any],
    path: str,
    key: str,
    opts: DiffOptions,
    out: list[Difference],
) -> None:
    """Align two collections by identity and diff the counterparts."""
    base_map = {item[key]: item for item in base}
    cand_map = {item[key]: item for item in cand}

    for identity in base_map:
        if identity not in cand_map:
            out.append(
                Difference(
                    path=_keyed_path(path, key, identity),
                    kind="MISSING_IN_CANDIDATE",
                    baseline=base_map[identity],
                    candidate=None,
                    detail=f"item {key}={identity!r} is returned by baseline but not by candidate",
                )
            )
    for identity in cand_map:
        if identity not in base_map:
            out.append(
                Difference(
                    path=_keyed_path(path, key, identity),
                    kind="EXTRA_IN_CANDIDATE",
                    baseline=None,
                    candidate=cand_map[identity],
                    detail=f"item {key}={identity!r} is returned by candidate but not by baseline",
                )
            )

    common = [identity for identity in base_map if identity in cand_map]

    # Ordering is reported once, over the items both sides returned. Whether it
    # counts as a regression depends on whether order is part of the contract.
    base_order = [item[key] for item in base if item[key] in cand_map]
    cand_order = [item[key] for item in cand if item[key] in base_map]
    if base_order != cand_order:
        out.append(
            Difference(
                path=path,
                kind="ORDER_ONLY" if opts.ignore_array_order else "ORDER",
                baseline=base_order[:12],
                candidate=cand_order[:12],
                detail=f"the same items come back in a different order (by {key})",
            )
        )

    for identity in common:
        _diff(
            base_map[identity],
            cand_map[identity],
            _keyed_path(path, key, identity),
            opts,
            out,
        )


def _keyed_path(path: str, key: str, identity: Any) -> str:
    return f"{path}[{key}={identity}]"


def canonical(value: Any, mask_paths: list[str] | None = None, path: str = "$") -> str:
    """Deterministic string form of ``value`` with masked paths blanked out.

    Used both for set comparison inside arrays and as the body fingerprint that
    flakiness triage compares across repeated calls.
    """
    return json.dumps(
        _blank(value, mask_paths or [], path), sort_keys=True, separators=(",", ":"), default=str
    )


def _blank(value: Any, patterns: list[str], path: str) -> Any:
    if is_masked(path, patterns):
        return MASKED
    if isinstance(value, dict):
        return {k: _blank(v, patterns, _child_path(path, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_blank(item, patterns, path + "[]") for item in value]
    return value


def _child_path(path: str, key: str) -> str:
    if key and key.replace("_", "").replace("-", "").isalnum() and not key[:1].isdigit():
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key)}]"


def _numeric_pair(left: str, right: str) -> bool:
    return {left, right} <= {"integer", "number"}


def _close(left: Any, right: Any, tolerance: float) -> bool:
    try:
        if tolerance <= 0:
            return float(left) == float(right)
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def _truncate(value: Any, limit: int = 300) -> Any:
    """Keep evidence readable: long strings and big containers are summarised."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... (+{len(value) - limit} chars)"
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, default=str)
        if len(rendered) > limit:
            kind = "object" if isinstance(value, dict) else "array"
            return f"<{kind} with {len(value)} entries, {len(rendered)} bytes>"
    return value
