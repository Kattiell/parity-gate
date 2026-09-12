"""Recorded contracts: the shape an API had on the day you decided it was right.

Comparing two live services only works while both are running, which is a
migration and nothing else. Most teams have exactly one API and still need to
know whether today's deploy broke the shape yesterday's consumers were built
against. That is what a recorded contract is for: run ``parity-gate record``
once, commit the file, and every later run is gated against it.

A contract stores **shape, not values** — types, requiredness and the status
codes an endpoint answered with. Values are deliberately left out: a recorded
value goes stale the moment the catalogue changes, and a suite that fails
because a product was renamed is a suite people learn to re-record without
reading. The reasoning is in ADR-001.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path as FilePath
from typing import Any

from parity_gate import __version__
from parity_gate.schema import Schema

SCHEMA_VERSION = 1


class ContractError(Exception):
    """A contract file is missing, malformed, or from an incompatible version."""


@dataclass
class RecordedCase:
    """What one case looked like when the contract was recorded."""

    schema: Schema
    statuses: list[int] = field(default_factory=list)
    stability: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "statuses": sorted(set(self.statuses)),
            "stability": self.stability,
            "schema": self.schema.to_portable(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecordedCase:
        return cls(
            schema=Schema.from_portable(data.get("schema", {})),
            statuses=[int(s) for s in data.get("statuses", [])],
            stability=str(data.get("stability", "UNKNOWN")),
        )


@dataclass
class Contract:
    """A whole suite's worth of recorded shapes."""

    suite_name: str
    source_url: str
    recorded_at: str
    tool_version: str = __version__
    cases: dict[str, RecordedCase] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "tool_version": self.tool_version,
            "suite": self.suite_name,
            "recorded_from": self.source_url,
            "recorded_at": self.recorded_at,
            "note": self.note,
            "cases": {case_id: case.to_dict() for case_id, case in sorted(self.cases.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Contract:
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ContractError(
                f"contract schema_version {version!r} is not supported by "
                f"parity-gate {__version__} (expected {SCHEMA_VERSION}). Re-record it."
            )
        return cls(
            suite_name=str(data.get("suite", "")),
            source_url=str(data.get("recorded_from", "")),
            recorded_at=str(data.get("recorded_at", "")),
            tool_version=str(data.get("tool_version", "")),
            note=str(data.get("note", "")),
            cases={
                case_id: RecordedCase.from_dict(raw)
                for case_id, raw in (data.get("cases") or {}).items()
            },
        )


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def save(contract: Contract, path: FilePath) -> FilePath:
    """Write a contract as LF-terminated JSON, ready to be committed.

    One field per line, because this file gets reviewed in pull requests and
    the whole value of the review is seeing ``"types": ["number"]`` become
    ``["string"]`` at a glance. Spread over eight lines each, nobody reads it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(contract.to_dict()) + "\n", encoding="utf-8", newline="\n")
    return path


def _dump(data: dict[str, Any]) -> str:
    """Standard indented JSON, except each field entry stays on one line."""
    placeholders: dict[str, str] = {}
    for case in data.get("cases", {}).values():
        fields = case.get("schema", {}).get("fields", [])
        compacted = []
        for entry in fields:
            token = f"\x00FIELD{len(placeholders)}\x00"
            placeholders[token] = json.dumps(entry, ensure_ascii=False, separators=(", ", ": "))
            compacted.append(token)
        case["schema"]["fields"] = compacted

    text = json.dumps(data, indent=2, ensure_ascii=False)
    for token, compact in placeholders.items():
        text = text.replace(json.dumps(token), compact)
    return text


def load(path: FilePath) -> Contract:
    """Read a recorded contract, with errors that say what to do about it."""
    if not path.is_file():
        raise ContractError(
            f"no recorded contract at {path}. Record one first:\n"
            f"    parity-gate record --suite <suite.toml>"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path}: not valid JSON ({exc})") from exc
    return Contract.from_dict(data)
