"""Suite loading: the declarative side of the tool.

A suite is a TOML file that names two targets (the contract consumers were
written against, and the thing that is replacing it), a safety policy, and the
cases to compare. TOML is read with :mod:`tomllib` from the standard library,
so a suite costs zero dependencies.

Validation is strict and the error messages name the offending case, because a
harness that fails with ``KeyError: 'method'`` teaches people to distrust it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from parity_gate import graphql
from parity_gate.differ import DiffOptions
from parity_gate.safety import Policy, scan_for_secrets

VALID_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})
VALID_RISKS = ("low", "medium", "high", "critical")


class SuiteError(Exception):
    """A suite file is malformed, unsafe, or internally inconsistent."""


@dataclass
class Target:
    """One side of the comparison.

    A target is either **live** (``base_url``) or a **recorded contract**
    (``snapshot``). Only the baseline may be a recording: there has to be
    something running to check.
    """

    name: str
    base_url: str = ""
    snapshot: str = ""
    auth_env: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_recorded(self) -> bool:
        return bool(self.snapshot)

    @property
    def label(self) -> str:
        return self.snapshot if self.is_recorded else self.base_url

    def resolved_headers(self) -> dict[str, str]:
        """Headers for this target, with the credential pulled from the env.

        The suite only ever stores the *name* of the variable. The value is
        read here, used, and never written to an evidence file.
        """
        headers = dict(self.headers)
        if self.auth_env:
            value = os.environ.get(self.auth_env)
            if not value:
                raise SuiteError(
                    f"target {self.name!r} needs the environment variable {self.auth_env!r}, "
                    "which is unset. Copy .env.example and export it before running."
                )
            headers[self.auth_header] = f"{self.auth_scheme} {value}".strip()
        return headers

    def url_for(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")


@dataclass
class Case:
    """One request, run against both targets and compared."""

    id: str
    title: str
    method: str
    path: str
    requirement: str | None = None
    risk: str = "medium"
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    expect_status: list[int] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    forbidden_fields: list[str] = field(default_factory=list)
    max_latency_ms: float | None = None
    mask_paths: list[str] = field(default_factory=list)
    array_keys: dict[str, str] = field(default_factory=dict)
    mutating: bool = False
    repeats: int | None = None
    #: A GraphQL document. When set, the case is a POST to ``path`` carrying it.
    graphql: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    operation_name: str | None = None
    #: Set on a case that is supposed to come back with GraphQL errors.
    expect_graphql_errors: bool = False

    @property
    def is_graphql(self) -> bool:
        return self.graphql is not None

    @property
    def graphql_operation(self) -> str | None:
        return graphql.operation_type(self.graphql) if self.graphql else None

    @property
    def reads_only(self) -> bool:
        """Whether this case cannot change anything, method notwithstanding."""
        return self.is_graphql and graphql.is_read(self.graphql or "")

    def request_body(self) -> Any:
        """What actually goes on the wire."""
        if self.is_graphql:
            return graphql.build_body(self.graphql or "", self.variables, self.operation_name)
        return self.body

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "requirement": self.requirement,
            "risk": self.risk,
            "method": self.method,
            "path": self.path,
            "mutating": self.mutating,
            "graphql": self.graphql_operation,
        }


@dataclass
class Suite:
    """A parsed, validated suite file."""

    name: str
    description: str
    source: Path
    baseline: Target
    candidate: Target
    policy: Policy
    cases: list[Case]
    repeats: int = 3
    mask_paths: list[str] = field(default_factory=list)
    array_keys: dict[str, str] = field(default_factory=dict)
    ignore_array_order: bool = True
    float_tolerance: float = 0.0

    def diff_options(self, case: Case) -> DiffOptions:
        """Suite-level masks plus the case's own, which is the common shape."""
        return DiffOptions(
            mask_paths=[*self.mask_paths, *case.mask_paths],
            array_keys={**self.array_keys, **case.array_keys},
            ignore_array_order=self.ignore_array_order,
            float_tolerance=self.float_tolerance,
        )

    def repeats_for(self, case: Case) -> int:
        return case.repeats if case.repeats is not None else self.repeats

    def select(self, patterns: list[str]) -> list[Case]:
        """Cases matching any of ``patterns``; all of them when none is given.

        A pattern is a glob against the case id or its requirement, or a
        case-insensitive substring of the title. One shape covers "just this
        case", "everything for this requirement" and "anything about orders",
        which is what people actually want at three in the afternoon with a
        two-hundred-case suite and one failing endpoint.
        """
        if not patterns:
            return list(self.cases)

        chosen: list[Case] = []
        for case in self.cases:
            for pattern in patterns:
                if (
                    fnmatch(case.id, pattern)
                    or (case.requirement and fnmatch(case.requirement, pattern))
                    or pattern.lower() in case.title.lower()
                ):
                    chosen.append(case)
                    break
        return chosen

    @property
    def contract_path(self) -> Path | None:
        """Where the recorded contract lives, if the baseline is a recording.

        Resolved relative to the suite file, so a suite directory can be moved
        or checked out anywhere and still find its own contracts.
        """
        if not self.baseline.is_recorded:
            return None
        candidate = Path(self.baseline.snapshot)
        return candidate if candidate.is_absolute() else self.source.parent / candidate


def load(path: str | Path) -> Suite:
    """Read, scan and validate a suite file."""
    source = Path(path)
    if not source.is_file():
        raise SuiteError(f"suite file not found: {source}")

    raw_text = source.read_text(encoding="utf-8")
    leaks = scan_for_secrets(raw_text, origin=str(source))
    if leaks:
        raise SuiteError(
            "refusing to run: the suite file contains what looks like a live credential.\n  "
            + "\n  ".join(leaks)
            + '\nMove it to an environment variable and reference it as auth = "env:NAME".'
            + "\nIf it was ever committed, rotate it: git history keeps it forever."
        )

    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise SuiteError(f"{source}: invalid TOML: {exc}") from exc

    return _build(data, source)


def _build(data: dict[str, Any], source: Path) -> Suite:
    name = str(data.get("name") or source.stem)
    description = str(data.get("description") or "")

    targets = data.get("targets")
    if not isinstance(targets, dict) or "baseline" not in targets or "candidate" not in targets:
        raise SuiteError(f"{source}: [targets.baseline] and [targets.candidate] are both required")

    baseline = _target("baseline", targets["baseline"], source)
    candidate = _target("candidate", targets["candidate"], source)

    policy_raw = data.get("policy") or {}
    if not isinstance(policy_raw, dict):
        raise SuiteError(f"{source}: [policy] must be a table")

    policy = Policy(
        allowed_hosts=[str(h) for h in policy_raw.get("allowed_hosts", [])],
        allow_mutations=bool(policy_raw.get("allow_mutations", False)),
        allow_production=bool(policy_raw.get("allow_production", False)),
        allow_private_networks=bool(policy_raw.get("allow_private_networks", False)),
        timeout_seconds=float(policy_raw.get("timeout_seconds", 10.0)),
        max_response_bytes=int(policy_raw.get("max_response_bytes", Policy.max_response_bytes)),
        max_retries=int(policy_raw.get("max_retries", 0)),
        workers=max(1, int(policy_raw.get("workers", 1))),
    )
    if policy.timeout_seconds <= 0:
        raise SuiteError(f"{source}: policy.timeout_seconds must be > 0")
    if policy.max_response_bytes < 1:
        raise SuiteError(f"{source}: policy.max_response_bytes must be >= 1")
    if "forbidden_host_patterns" in policy_raw:
        policy.forbidden_host_patterns = [str(p) for p in policy_raw["forbidden_host_patterns"]]
    if not policy.allowed_hosts:
        raise SuiteError(
            f"{source}: policy.allowed_hosts is empty. "
            "List the hosts this suite is allowed to reach; there is no implicit allow-all."
        )

    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SuiteError(f"{source}: at least one [[cases]] entry is required")

    cases = [_case(entry, index, source) for index, entry in enumerate(raw_cases)]
    duplicates = _duplicates([c.id for c in cases])
    if duplicates:
        raise SuiteError(f"{source}: duplicate case id(s): {', '.join(sorted(duplicates))}")

    repeats = int(policy_raw.get("repeats", 3))
    if repeats < 1:
        raise SuiteError(f"{source}: policy.repeats must be >= 1")

    return Suite(
        name=name,
        description=description,
        source=source,
        baseline=baseline,
        candidate=candidate,
        policy=policy,
        cases=cases,
        repeats=repeats,
        mask_paths=[str(p) for p in policy_raw.get("mask_paths", [])],
        array_keys={str(k): str(v) for k, v in (policy_raw.get("array_keys") or {}).items()},
        ignore_array_order=bool(policy_raw.get("ignore_array_order", True)),
        float_tolerance=float(policy_raw.get("float_tolerance", 0.0)),
    )


def _target(name: str, raw: Any, source: Path) -> Target:
    if not isinstance(raw, dict):
        raise SuiteError(f"{source}: [targets.{name}] must be a table")

    base_url = str(raw.get("base_url") or "").strip()
    snapshot = str(raw.get("snapshot") or "").strip()

    if base_url and snapshot:
        raise SuiteError(
            f"{source}: targets.{name} sets both base_url and snapshot. "
            "A target is either a live service or a recorded contract, not both."
        )
    if not base_url and not snapshot:
        raise SuiteError(
            f"{source}: targets.{name} needs either base_url (a live service) "
            'or snapshot (a recorded contract, e.g. snapshot = "contracts/api.json")'
        )
    if snapshot and name != "baseline":
        raise SuiteError(
            f"{source}: only targets.baseline may be a recorded contract; "
            f"targets.{name} has to be a running service for there to be anything to check"
        )

    auth_env: str | None = None
    auth = raw.get("auth")
    if auth is not None:
        if not isinstance(auth, str) or not auth.startswith("env:"):
            raise SuiteError(
                f'{source}: targets.{name}.auth must look like "env:VARIABLE_NAME". '
                "Credentials are never stored in the suite itself."
            )
        auth_env = auth[4:].strip()
        if not auth_env:
            raise SuiteError(f"{source}: targets.{name}.auth is missing a variable name")

    return Target(
        name=name,
        base_url=base_url,
        snapshot=snapshot,
        auth_env=auth_env,
        auth_header=str(raw.get("auth_header", "Authorization")),
        auth_scheme=str(raw.get("auth_scheme", "Bearer")),
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
    )


def _case(raw: Any, index: int, source: Path) -> Case:
    where = f"{source}: [[cases]] #{index + 1}"
    if not isinstance(raw, dict):
        raise SuiteError(f"{where} must be a table")

    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise SuiteError(f"{where} is missing a string `id`")
    where = f"{source}: case {case_id}"

    document = raw.get("graphql")
    if document is not None and not str(document).strip():
        raise SuiteError(f"{where}: `graphql` is present but empty")

    method = str(raw.get("method", "POST" if document else "GET")).upper()
    if method not in VALID_METHODS:
        raise SuiteError(f"{where}: method {method!r} is not one of {sorted(VALID_METHODS)}")
    if document is not None:
        if method != "POST":
            raise SuiteError(
                f"{where}: a GraphQL case is always a POST; remove `method` or set it to POST"
            )
        if raw.get("body") is not None:
            raise SuiteError(
                f"{where}: set either `graphql` or `body`, not both; the body is built "
                "from the document, the variables and the operation name"
            )
        if graphql.operation_type(str(document)) == graphql.SUBSCRIPTION:
            raise SuiteError(
                f"{where}: subscriptions are long-lived streams and are not supported; "
                "gate the query and mutation operations instead"
            )

    path = raw.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise SuiteError(f"{where}: `path` is required and must start with '/'")

    risk = str(raw.get("risk", "medium")).lower()
    if risk not in VALID_RISKS:
        raise SuiteError(f"{where}: risk {risk!r} is not one of {list(VALID_RISKS)}")

    expect = raw.get("expect_status", [])
    if isinstance(expect, int):
        expect = [expect]
    if not isinstance(expect, list) or not all(isinstance(s, int) for s in expect):
        raise SuiteError(f"{where}: expect_status must be an integer or a list of integers")

    repeats = raw.get("repeats")
    if repeats is not None and (not isinstance(repeats, int) or repeats < 1):
        raise SuiteError(f"{where}: repeats must be an integer >= 1")

    return Case(
        id=case_id.strip(),
        title=str(raw.get("title") or case_id),
        method=method,
        path=path,
        requirement=(str(raw["requirement"]) if raw.get("requirement") else None),
        risk=risk,
        body=raw.get("body"),
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
        expect_status=list(expect),
        required_fields=[str(p) for p in raw.get("required_fields", [])],
        forbidden_fields=[str(p) for p in raw.get("forbidden_fields", [])],
        max_latency_ms=(float(raw["max_latency_ms"]) if raw.get("max_latency_ms") else None),
        mask_paths=[str(p) for p in raw.get("mask_paths", [])],
        array_keys={str(k): str(v) for k, v in (raw.get("array_keys") or {}).items()},
        mutating=bool(raw.get("mutating", False)),
        repeats=repeats,
        graphql=(str(document) if document is not None else None),
        variables=dict(raw.get("variables") or {}),
        operation_name=(str(raw["operation_name"]) if raw.get("operation_name") else None),
        expect_graphql_errors=bool(raw.get("expect_graphql_errors", False)),
    )


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return dupes
