"""SARIF 2.1.0 output, so findings land where code review already happens.

SARIF is the OASIS interchange format for analysis results. GitHub code
scanning, Azure DevOps and the common IDE viewers all read it, which means a
breaking drift can appear as an annotation on the pull request that caused it,
next to the line that describes it, instead of in an artifact someone has to
download and open.

Where a finding points:

* **Contract mode:** at the field's own line in the recorded contract, when
  there is one. The contract is written one field per line precisely so a diff
  of it is readable, and that makes it the natural place for the annotation.
* **Otherwise:** at the case's ``id = "..."`` line in the suite file.

Each result carries a ``partialFingerprints`` entry built from the case, the
rule and the index-free path, so the same finding on the next run is recognised
as the same alert rather than a new one, and a fixed finding closes.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from parity_gate import __version__, rules
from parity_gate.evidence import ERROR, Record, Run

SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
RULES_URI = "https://github.com/Kattiell/parity-gate/blob/trunk/docs/rules.md"

_SEVERITY_LEVEL = {"breaking": "error", "risky": "warning", "additive": "note"}
_SUBSCRIPT = re.compile(r"\[[^\]]*\]")


def render(run: Run, *, root: Path | None = None) -> dict[str, Any]:
    """The SARIF log for one run. Paths are made relative to ``root`` (cwd)."""
    base = (root or Path.cwd()).resolve()
    suite_file = Path(run.suite_path)
    locator = _Locator(base, suite_file)

    results: list[dict[str, Any]] = []
    for record in run.records:
        results.extend(_results_for(record, locator, run.suite_name))

    return {
        "$schema": SCHEMA_URI,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "parity-gate",
                        "semanticVersion": __version__,
                        "informationUri": "https://github.com/Kattiell/parity-gate",
                        "rules": [_rule_descriptor(rule) for rule in rules.RULES],
                    }
                },
                # Keeps two suites uploaded from one repository from closing
                # each other's alerts.
                "automationDetails": {"id": f"parity-gate/{run.suite_name}/"},
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "startTimeUtc": run.started_at,
                        "endTimeUtc": run.finished_at or run.started_at,
                    }
                ],
                "properties": {
                    "runId": run.run_id,
                    "mode": run.mode,
                    "verdict": run.verdict,
                    "chainHead": run.chain_head,
                },
                "results": results,
            }
        ],
    }


def _rule_descriptor(rule: rules.Rule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "shortDescription": {"text": rule.summary},
        "fullDescription": {"text": rule.help},
        "help": {"text": rule.help},
        "helpUri": f"{RULES_URI}#{rule.id.lower()}",
        "defaultConfiguration": {"level": rule.level},
        "properties": {"family": rule.family},
    }


def _results_for(record: Record, locator: _Locator, suite_name: str) -> list[dict[str, Any]]:
    case = record.case
    case_id = str(case.get("id"))
    contract_path = record.baseline.get("path") if isinstance(record.baseline, dict) else None
    found: list[dict[str, Any]] = []

    def add(
        rule_id: str | None,
        level: str,
        message: str,
        path: str = "$",
        where: dict[str, Any] | None = None,
        waiver: dict[str, Any] | None = None,
    ) -> None:
        if rule_id is None:
            return
        result: dict[str, Any] = {
            "ruleId": rule_id,
            "level": level,
            "message": {"text": f"{case_id}: {message}"},
            "locations": [where or locator.case(case_id)],
            "partialFingerprints": {
                "parityGate/v1": _fingerprint(suite_name, case_id, rule_id, path)
            },
            "properties": {
                "case": case_id,
                "requirement": case.get("requirement"),
                "risk": case.get("risk"),
                "path": path,
                "verdict": record.verdict,
            },
        }
        if waiver:
            result["suppressions"] = [
                {
                    "kind": "external",
                    "status": "accepted",
                    "justification": (
                        f"{waiver.get('reason')} (owner {waiver.get('owner')}, "
                        f"expires {waiver.get('expires')}, ticket {waiver.get('ticket') or '-'})"
                    ),
                }
            ]
        found.append(result)

    for check in record.checks:
        if not check.get("passed"):
            add(
                check.get("rule"),
                "error",
                check.get("detail") or check["name"],
                waiver=check.get("waiver"),
            )

    for drift in record.drifts:
        where = (
            locator.contract_field(contract_path, case_id, drift["path"]) if contract_path else None
        )
        add(
            drift.get("rule"),
            _SEVERITY_LEVEL.get(drift["severity"], "warning"),
            f"{drift['kind']} ({drift['severity']}) at {drift['path']}: {drift['detail']}",
            drift["path"],
            where,
            drift.get("waiver"),
        )

    for difference in record.differences:
        level = "note" if difference["kind"] == "ORDER_ONLY" else "error"
        before = _short(difference.get("baseline"))
        after = _short(difference.get("candidate"))
        add(
            difference.get("rule"),
            level,
            f"{difference['kind']} at {difference['path']}: baseline {before}, candidate {after}",
            _SUBSCRIPT.sub("[]", difference["path"]),
            waiver=difference.get("waiver"),
        )

    unstable = False
    for side in ("baseline", "candidate"):
        stability = record.stability.get(side) if isinstance(record.stability, dict) else None
        verdict = (stability or {}).get("verdict")
        if verdict in {"FLAKY_STATUS", "FLAKY_SHAPE", "VOLATILE_BODY"}:
            unstable = unstable or verdict != "VOLATILE_BODY"
            detail = (stability or {}).get("detail", "")
            if verdict != "VOLATILE_BODY" and record.error:
                detail = record.error
            add(
                rules.rule_id(rules.STABILITY, verdict),
                "note" if verdict == "VOLATILE_BODY" else "warning",
                f"{side} {verdict}: {detail}",
            )

    if record.error and not unstable:
        if record.verdict == ERROR:
            add(rules.rule_id(rules.RUN, "run_error"), "error", record.error)
        else:
            add(rules.rule_id(rules.RUN, "not_gated"), "warning", record.error)

    return found


class _Locator:
    """Finds the line a finding belongs on, falling back to the suite case."""

    def __init__(self, root: Path, suite_file: Path) -> None:
        self.root = root
        self.suite_file = suite_file
        self._lines: dict[Path, list[str]] = {}

    def case(self, case_id: str) -> dict[str, Any]:
        pattern = re.compile(rf"""^\s*id\s*=\s*["']{re.escape(case_id)}["']""")
        return self._location(self.suite_file, self._find(self.suite_file, pattern))

    def contract_field(self, contract: str, case_id: str, path: str) -> dict[str, Any]:
        file = Path(contract)
        lines = self._read(file)
        start = next(
            (i for i, line in enumerate(lines) if line.strip().startswith(f'"{case_id}": {{')),
            None,
        )
        if start is None:
            return self.case(case_id)
        needle = f'"path": "{path}"'
        for index in range(start, len(lines)):
            if needle in lines[index]:
                return self._location(file, index + 1)
            if index > start and re.match(r'^    "[^"]+": \{', lines[index]):
                break  # the next case began; the field is not recorded (e.g. it is new)
        return self._location(file, start + 1)

    def _find(self, file: Path, pattern: re.Pattern[str]) -> int:
        for index, line in enumerate(self._read(file)):
            if pattern.search(line):
                return index + 1
        return 1

    def _read(self, file: Path) -> list[str]:
        if file not in self._lines:
            try:
                self._lines[file] = file.read_text(encoding="utf-8").splitlines()
            except OSError:
                self._lines[file] = []
        return self._lines[file]

    def _location(self, file: Path, line: int) -> dict[str, Any]:
        return {
            "physicalLocation": {
                "artifactLocation": self._artifact(file),
                "region": {"startLine": max(1, line)},
            }
        }

    def _artifact(self, file: Path) -> dict[str, Any]:
        resolved = file if file.is_absolute() else (Path.cwd() / file)
        try:
            relative = resolved.resolve().relative_to(self.root)
        except ValueError:
            return {"uri": resolved.resolve().as_uri()}
        return {"uri": relative.as_posix(), "uriBaseId": "%SRCROOT%"}


def _fingerprint(suite: str, case_id: str, rule_id: str, path: str) -> str:
    text = "\x00".join((suite, case_id, rule_id, _SUBSCRIPT.sub("[]", path)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _short(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
