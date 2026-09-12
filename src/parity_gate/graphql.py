"""GraphQL support, which is mostly about two things HTTP gets for free.

A GraphQL endpoint is JSON over HTTP, so the contract engine already applies to
it unchanged. What does not carry over is everything the tool infers from the
*method* and the *status code*:

**Every operation is a POST.** Reads and writes look identical from outside, so
the write gate — which refuses a POST unless the case declares ``mutating`` and
the run passes ``--allow-mutations`` — would force every read to be declared a
write, and a safety control that everyone has to switch off protects nothing.
The operation type is in the query text, so it is read from there.

**Every response is 200**, including the failures. A GraphQL server reports
errors in the body, so ``recorded_status`` — the check that catches a 404
softened into a 200 — has nothing to work with. The equivalent signal is the
``errors`` array: a query that used to return none and now returns one is the
same class of regression, and it is invisible to every status-based check.
"""

from __future__ import annotations

import re
from typing import Any

#: Comments, then the first meaningful token, decide the operation type.
_COMMENT = re.compile(r"#[^\n\r]*")
_OPERATION = re.compile(r"^\s*(query|mutation|subscription)\b", re.IGNORECASE)

READ = "query"
WRITE = "mutation"
SUBSCRIPTION = "subscription"


def operation_type(document: str) -> str:
    """Whether a GraphQL document reads, writes, or subscribes.

    An anonymous document — one that starts with ``{`` — is shorthand for a
    query, which is the form most examples use and the one most likely to be
    pasted into a suite.
    """
    stripped = _COMMENT.sub("", document or "").strip()
    if not stripped:
        return READ
    match = _OPERATION.match(stripped)
    if match:
        return match.group(1).lower()
    return READ if stripped.startswith("{") else WRITE


def is_read(document: str) -> bool:
    """True for a query: safe to run without the mutation gate."""
    return operation_type(document) == READ


def build_body(
    document: str, variables: dict[str, Any] | None, operation_name: str | None
) -> dict[str, Any]:
    """The POST body a GraphQL server expects."""
    body: dict[str, Any] = {"query": document}
    if variables:
        body["variables"] = variables
    if operation_name:
        body["operationName"] = operation_name
    return body


def errors_in(payload: Any) -> list[Any]:
    """The ``errors`` entries in a GraphQL response, if any."""
    if isinstance(payload, dict):
        found = payload.get("errors")
        if isinstance(found, list):
            return found
        if found:
            return [found]
    return []


def describe(errors: list[Any]) -> str:
    """A one-line summary of GraphQL errors, for a check detail."""
    messages = []
    for entry in errors[:3]:
        if isinstance(entry, dict) and entry.get("message"):
            messages.append(str(entry["message"]))
        else:
            messages.append(str(entry))
    summary = "; ".join(messages)
    if len(errors) > 3:
        summary += f" (+{len(errors) - 3} more)"
    return summary
