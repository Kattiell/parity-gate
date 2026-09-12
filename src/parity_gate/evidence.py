"""The evidence trail.

A QA run that cannot be reconstructed later is an opinion. Every case produces
a record, every record is hashed together with the hash of the one before it,
and the chain head is written to a manifest. Editing a result after the fact
breaks the chain, and ``parity-gate verify`` says exactly where.

This is not cryptographic proof of honesty -- whoever can edit the records can
recompute the chain. It is proof of *accidental* alteration: a truncated
upload, a partially synced artifact, a report edited by hand before being
pasted into a ticket. Those are the realistic failure modes of test evidence,
and they are currently invisible.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from parity_gate import __version__
from parity_gate.redaction import redact, redact_headers

GENESIS = "0" * 64
MAX_BODY_CHARS = 20_000

PASS = "PASS"  # noqa: S105 - a verdict name, not a credential
FAIL = "FAIL"
WARN = "WARN"
ERROR = "ERROR"
SKIPPED = "SKIPPED"

VERDICT_RANK = {PASS: 0, SKIPPED: 1, WARN: 2, FAIL: 3, ERROR: 4}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.urandom(3).hex()}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def capture_exchange(response: Any, headers_sent: dict[str, str]) -> dict[str, Any]:
    """Turn one HTTP exchange into a redacted, size-capped evidence fragment."""
    body = redact(response.json_body) if response.json_body is not None else None
    text = response.body_text or ""
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"... (truncated, {len(response.body_text)} chars total)"
    return {
        **response.to_dict(),
        "request_headers": redact_headers(headers_sent),
        "response_headers": redact_headers(response.headers),
        "body": body,
        "body_text_sha256": sha256_text(response.body_text or ""),
        "body_text_preview": redact(text) if body is None else None,
    }


@dataclass
class Record:
    """One case, both sides, every finding, plus its place in the hash chain."""

    case: dict[str, Any]
    verdict: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    drifts: list[dict[str, Any]] = field(default_factory=list)
    differences: list[dict[str, Any]] = field(default_factory=list)
    differences_suppressed: int = 0
    #: Paths nothing could be learned about, because a collection was empty in
    #: every sample. Not findings, but not silence either.
    unchecked_paths: list[str] = field(default_factory=list)
    stability: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    candidate: dict[str, Any] = field(default_factory=dict)
    schema: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    previous_hash: str = GENESIS
    record_hash: str = ""

    def payload(self) -> dict[str, Any]:
        """The hashed content: everything except the hash fields themselves."""
        return {
            "case": self.case,
            "verdict": self.verdict,
            "checks": self.checks,
            "drifts": self.drifts,
            "differences": self.differences,
            "differences_suppressed": self.differences_suppressed,
            "unchecked_paths": self.unchecked_paths,
            "stability": self.stability,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "schema": self.schema,
            "error": self.error,
        }

    def seal(self, previous_hash: str) -> str:
        """Link this record to the previous one and freeze its hash."""
        self.previous_hash = previous_hash
        body = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"), default=str)
        self.record_hash = sha256_text(previous_hash + body)
        return self.record_hash

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "previous_hash": self.previous_hash, "hash": self.record_hash}


@dataclass
class Run:
    """A whole execution: metadata, sealed records and the roll-up."""

    run_id: str
    suite_name: str
    suite_path: str
    suite_sha256: str
    baseline_url: str
    candidate_url: str
    policy: dict[str, Any]
    #: Which comparison this run performed. A contract run never looks at
    #: values, so reporting "0 value differences" without saying so would read
    #: as "no values changed" instead of "values were not checked".
    mode: str = "differential"
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    records: list[Record] = field(default_factory=list)

    def add(self, record: Record) -> None:
        previous = self.records[-1].record_hash if self.records else GENESIS
        record.seal(previous)
        self.records.append(record)

    @property
    def chain_head(self) -> str:
        return self.records[-1].record_hash if self.records else GENESIS

    @property
    def verdict(self) -> str:
        if not self.records:
            return SKIPPED
        return max((r.verdict for r in self.records), key=lambda v: VERDICT_RANK.get(v, 0))

    def counts(self) -> dict[str, int]:
        tally = dict.fromkeys(VERDICT_RANK, 0)
        for record in self.records:
            tally[record.verdict] = tally.get(record.verdict, 0) + 1
        return tally

    def traceability(self) -> dict[str, list[dict[str, str]]]:
        """Requirement id -> the cases that exercised it and how they ended."""
        matrix: dict[str, list[dict[str, str]]] = {}
        for record in self.records:
            requirement = record.case.get("requirement") or "(unmapped)"
            matrix.setdefault(requirement, []).append(
                {
                    "case": str(record.case.get("id")),
                    "title": str(record.case.get("title")),
                    "risk": str(record.case.get("risk")),
                    "verdict": record.verdict,
                }
            )
        return dict(sorted(matrix.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "tool": {
                "name": "parity-gate",
                "version": __version__,
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
            "run_id": self.run_id,
            "mode": self.mode,
            "suite": {
                "name": self.suite_name,
                "path": self.suite_path,
                "sha256": self.suite_sha256,
            },
            "targets": {"baseline": self.baseline_url, "candidate": self.candidate_url},
            "policy": self.policy,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": {
                "verdict": self.verdict,
                "cases": len(self.records),
                "by_verdict": self.counts(),
                "breaking_drifts": sum(
                    1 for r in self.records for d in r.drifts if d["severity"] == "breaking"
                ),
                "differences": (
                    sum(len(r.differences) for r in self.records)
                    if self.mode == "differential"
                    else None
                ),
                "values_compared": self.mode == "differential",
                "unstable_cases": sum(1 for r in self.records if _is_unstable(r)),
                "unchecked_paths": sum(len(r.unchecked_paths) for r in self.records),
                "volatile_cases": sum(1 for r in self.records if _is_volatile(r)),
            },
            "traceability": self.traceability(),
            "chain_head": self.chain_head,
            "records": [record.to_dict() for record in self.records],
        }


def write_text(path: Path, text: str) -> str:
    r"""Write UTF-8 with LF endings and return the file's SHA-256.

    Pinning ``newline="\n"`` is not cosmetic: without it Python translates line
    endings per platform, the same run produces different bytes on Windows and
    Linux, and the recorded hash stops matching the file as soon as the bundle
    crosses an OS or a git checkout. Evidence whose hash depends on where it was
    generated is not evidence.
    """
    path.write_text(text, encoding="utf-8", newline="\n")
    return sha256_file(path)


def write_run(run: Run, directory: Path) -> dict[str, str]:
    """Write ``run.json`` and return its entry for the manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    run_file = directory / "run.json"
    payload = json.dumps(run.to_dict(), indent=2, ensure_ascii=False, default=str)
    return {"run.json": write_text(run_file, payload)}


def write_manifest(directory: Path, files: dict[str, str], chain_head: str, run_id: str) -> Path:
    manifest = directory / "manifest.json"
    write_text(
        manifest,
        json.dumps(
            {
                "run_id": run_id,
                "generated_at": utc_now(),
                "tool_version": __version__,
                "chain_head": chain_head,
                "files": files,
            },
            indent=2,
        ),
    )
    return manifest


def verify(directory: Path) -> list[str]:
    """Re-derive the hash chain and the file hashes. Empty list means intact."""
    problems: list[str] = []
    manifest_path = directory / "manifest.json"
    run_path = directory / "run.json"

    if not manifest_path.is_file():
        return [f"{manifest_path} is missing"]
    if not run_path.is_file():
        return [f"{run_path} is missing"]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, expected in manifest.get("files", {}).items():
        target = directory / name
        if not target.is_file():
            problems.append(f"{name}: listed in the manifest but missing from disk")
            continue
        actual = sha256_file(target)
        if actual != expected:
            problems.append(
                f"{name}: sha256 mismatch (manifest {expected[:12]}, file {actual[:12]})"
            )

    data = json.loads(run_path.read_text(encoding="utf-8"))
    previous = GENESIS
    for index, raw in enumerate(data.get("records", [])):
        stored = raw.get("hash", "")
        payload = {key: raw.get(key) for key in _RECORD_KEYS}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        expected = sha256_text(previous + body)
        if raw.get("previous_hash") != previous:
            problems.append(
                f"record #{index + 1} ({raw.get('case', {}).get('id')}): "
                "previous_hash does not match the record before it"
            )
        if stored != expected:
            problems.append(
                f"record #{index + 1} ({raw.get('case', {}).get('id')}): "
                "content does not match its hash; this record was altered"
            )
        previous = stored

    if data.get("chain_head") != previous:
        problems.append("chain_head does not match the last record hash")

    return problems


def _is_unstable(record: Record) -> bool:
    """A case where at least one side answered inconsistently to identical calls."""
    return any(
        side.get("verdict") in {"FLAKY_STATUS", "FLAKY_SHAPE"}
        for side in record.stability.values()
        if isinstance(side, dict)
    )


def _is_volatile(record: Record) -> bool:
    """A case whose values move between identical calls: noisy, not broken."""
    return any(
        side.get("verdict") == "VOLATILE_BODY"
        for side in record.stability.values()
        if isinstance(side, dict)
    )


_RECORD_KEYS = (
    "case",
    "verdict",
    "checks",
    "drifts",
    "differences",
    "differences_suppressed",
    "unchecked_paths",
    "stability",
    "baseline",
    "candidate",
    "schema",
    "error",
)
