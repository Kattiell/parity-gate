"""Turn an OpenAPI document into a starter suite.

A maintained OpenAPI spec is usually a reason to reach for Schemathesis or
Dredd instead of this tool, and often it should be: they check the
implementation against the spec, which is a question this does not ask.

The reason to import one anyway is that the spec answers a different question
very cheaply — *which endpoints exist* — and writing that list out by hand is
the single largest piece of friction in adopting a contract gate. Thirty
endpoints is thirty blocks of TOML nobody wants to type.

There is also a third question neither tool asks on its own, and importing
makes it one command away: **does the spec still describe the service?** The
generated suite is recorded against the running API, so the recorded contract
and the spec are two independent descriptions of the same thing, and they can
be compared.

Only JSON is read. Most frameworks serve it live — ``/openapi.json`` for
FastAPI, ``/v3/api-docs`` for Spring — and adding a YAML parser to a
zero-dependency tool to save one conversion is a bad trade.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

SAFE_METHODS = ("get", "head")
WRITE_METHODS = ("post", "put", "patch", "delete")

_SLUG = re.compile(r"[^a-zA-Z0-9]+")
_PARAM = re.compile(r"\{([^}]+)\}")


class OpenAPIError(Exception):
    """The document is not something a suite can be generated from."""


def load_spec(text: str, origin: str) -> dict[str, Any]:
    """Parse an OpenAPI document, with an error that says what to do."""
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        hint = ""
        if text.lstrip()[:1] not in {"{", "["}:
            hint = (
                "\nThis looks like YAML. Only JSON is read — point at the document the "
                "service serves (commonly /openapi.json or /v3/api-docs), or convert it once."
            )
        raise OpenAPIError(f"{origin}: not valid JSON ({exc}){hint}") from exc

    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise OpenAPIError(f"{origin}: no `paths` object — this is not an OpenAPI document")
    return spec


def base_url_of(spec: dict[str, Any], fallback: str = "") -> str:
    """The first server URL the document declares, if any."""
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        url = str(servers[0].get("url") or "").strip()
        if url.startswith("http"):
            return url.rstrip("/")
    # Swagger 2.0 spelled it differently.
    host = str(spec.get("host") or "").strip()
    if host:
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{str(spec.get('basePath') or '').rstrip('/')}"
    return fallback.rstrip("/")


def _case_id(method: str, path: str, operation: dict[str, Any], taken: set[str]) -> str:
    raw = str(operation.get("operationId") or f"{method}-{path}")
    slug = _SLUG.sub("-", raw).strip("-").upper()[:40] or "CASE"
    candidate, suffix = slug, 2
    while candidate in taken:
        candidate, suffix = f"{slug}-{suffix}", suffix + 1
    taken.add(candidate)
    return candidate


def _title(method: str, path: str, operation: dict[str, Any]) -> str:
    summary = str(operation.get("summary") or "").strip()
    if not summary:
        summary = str(operation.get("description") or "").strip().split("\n")[0]
    return summary or f"{method.upper()} {path}"


def _requirement(operation: dict[str, Any]) -> str | None:
    tags = operation.get("tags")
    if isinstance(tags, list) and tags:
        return _SLUG.sub("-", str(tags[0])).strip("-").upper() or None
    return None


def _example_for(parameter: dict[str, Any]) -> Any:
    """A usable value for a path parameter, or None when the spec gives none."""
    for source in (parameter, parameter.get("schema") or {}):
        for key in ("example", "default"):
            if key in source:
                return source[key]
        examples = source.get("examples")
        if isinstance(examples, dict) and examples:
            first = next(iter(examples.values()))
            if isinstance(first, dict) and "value" in first:
                return first["value"]
    schema = parameter.get("schema") or {}
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    return None


def _fill_path(
    path: str, parameters: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """Substitute path parameters. Returns the path and what could not be filled."""
    by_name = {str(p.get("name")): p for p in parameters if p.get("in") == "path"}
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        example = _example_for(by_name.get(name, {}))
        if example is None:
            missing.append(name)
            return match.group(0)
        return str(example)

    return _PARAM.sub(replace, path), missing


def _toml_string(value: str) -> str:
    return json.dumps(value)


def to_suite(
    spec: dict[str, Any],
    *,
    name: str,
    base_url: str,
    contract_path: str,
    include_writes: bool = False,
) -> str:
    """Render a suite. Cases that cannot be filled in are emitted commented out."""
    resolved = (base_url or base_url_of(spec)).rstrip("/")
    if not resolved:
        raise OpenAPIError(
            "the document declares no server URL — pass --base-url with the address to test"
        )
    host = urlsplit(resolved).hostname or ""
    info = spec.get("info") or {}

    lines: list[str] = [
        f"# Generated by parity-gate from {info.get('title') or 'an OpenAPI document'}"
        + (f" {info.get('version')}" if info.get("version") else ""),
        "#",
        "# A starting point, not an answer. Read it before you record against it:",
        "#   - delete the endpoints whose breakage would not wake anybody up",
        "#   - fill in any path parameter marked TODO below",
        "#   - run `parity-gate stability` first, and paste back the masks it prints",
        "",
        f"name = {_toml_string(name)}",
        "",
        "[targets.baseline]",
        f"snapshot = {_toml_string(contract_path)}",
        "",
        "[targets.candidate]",
        f"base_url = {_toml_string(resolved)}",
        "",
        "[policy]",
        f"allowed_hosts = [{_toml_string(host)}]",
        "repeats = 3",
        "max_retries = 0",
        "timeout_seconds = 15",
        "",
    ]

    taken: set[str] = set()
    emitted = skipped = 0
    methods = SAFE_METHODS + (WRITE_METHODS if include_writes else ())

    for path, item in sorted((spec.get("paths") or {}).items()):
        if not isinstance(item, dict):
            continue
        shared = [p for p in (item.get("parameters") or []) if isinstance(p, dict)]
        for method in methods:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue

            parameters = shared + [
                p for p in (operation.get("parameters") or []) if isinstance(p, dict)
            ]
            filled, missing = _fill_path(str(path), parameters)
            case_id = _case_id(method, str(path), operation, taken)
            requirement = _requirement(operation)
            block: list[str] = ["[[cases]]", f"id = {_toml_string(case_id)}"]
            if requirement:
                block.append(f"requirement = {_toml_string(requirement)}")
            block.append(f"title = {_toml_string(_title(method, str(path), operation))}")
            block.append(f'method = "{method.upper()}"')
            block.append(f"path = {_toml_string(filled)}")
            if method in WRITE_METHODS:
                block.append("mutating = true   # needs --allow-mutations as well")

            if missing:
                skipped += 1
                lines.append(
                    f"# TODO: no example for path parameter(s) {', '.join(missing)} — "
                    "fill one in and uncomment."
                )
                lines.extend(f"# {line}" for line in block)
            else:
                emitted += 1
                lines.extend(block)
            lines.append("")

    if not emitted and not skipped:
        raise OpenAPIError("no operations found for the selected methods")

    lines.insert(
        6,
        f"# {emitted} case(s) ready to run"
        + (f", {skipped} commented out pending a path parameter" if skipped else ""),
    )
    return "\n".join(lines).rstrip() + "\n"
