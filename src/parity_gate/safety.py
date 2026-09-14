"""Pre-flight guards. A QA tool must never be the cause of the incident.

Three failure modes this module exists to prevent, all of them things that
happen to real teams:

1. A suite written against staging is run with a production base URL still in
   an environment variable, and the harness happily deletes live records.
2. A ``DELETE``/``POST`` case that was safe against a disposable fixture gets
   pointed at shared data.
3. A live token is pasted into a suite file "just to test it" and is then
   committed to a public repository.

Every one of these is cheap to prevent at the door and expensive to explain
afterwards, so the defaults are restrictive and opening them up is explicit.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from parity_gate.redaction import find_secrets

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_FORBIDDEN_HOST_PATTERNS = ("prod", "prd", "producao", "production")


class SafetyError(Exception):
    """Raised when a run would do something the policy forbids."""


@dataclass
class Policy:
    """The blast radius a run is allowed to have."""

    #: Hosts the run may talk to. ``example.com`` matches the host exactly;
    #: ``.example.com`` matches any subdomain. An empty list blocks everything.
    allowed_hosts: list[str] = field(default_factory=list)
    #: Non-idempotent methods are refused unless this is switched on *and* the
    #: individual case is marked ``mutating = true``.
    allow_mutations: bool = False
    #: Substrings that, found in a hostname, mean "this looks like production".
    forbidden_host_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_FORBIDDEN_HOST_PATTERNS)
    )
    #: Deliberate override, so that hitting production is a decision with a name.
    allow_production: bool = False
    #: Loopback and RFC1918 targets are allowed only on purpose (the bundled
    #: mock server runs on 127.0.0.1, so demo suites set this).
    allow_private_networks: bool = False
    #: A deadline for one whole exchange (request, headers and body), not for
    #: each socket read: a read timeout is reset by every byte that arrives, so
    #: a server trickling its body could otherwise hold a run open forever.
    timeout_seconds: float = 10.0
    #: Bodies larger than this are refused as a transport error rather than
    #: loaded into memory. Ten MiB is generous for a JSON API and small enough
    #: that a runaway export endpoint cannot take the CI job down with it.
    max_response_bytes: int = 10 * 1024 * 1024
    #: Zero by default, and deliberately so. Retrying a 503 turns an endpoint
    #: that fails one call in three into one that looks healthy, which destroys
    #: the flakiness signal this tool exists to surface. Raise it only for a
    #: suite where you would rather have the result than the truth about it.
    max_retries: int = 0
    #: How many cases run at once. One by default: a QA tool that quietly puts
    #: eight times the load on someone's staging environment is a bad guest.
    #: Raise it deliberately. The repeated calls *within* a case always stay
    #: sequential, whatever this is set to, because interleaving them would
    #: change what the stability measurement means.
    workers: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed_hosts": self.allowed_hosts,
            "allow_mutations": self.allow_mutations,
            "allow_production": self.allow_production,
            "allow_private_networks": self.allow_private_networks,
            "forbidden_host_patterns": self.forbidden_host_patterns,
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "max_retries": self.max_retries,
            "workers": self.workers,
        }


def host_allowed(host: str, allowed: list[str]) -> bool:
    """Exact match, or suffix match for entries written as ``.example.com``."""
    host = host.lower().rstrip(".")
    for entry in allowed:
        candidate = entry.lower().strip().rstrip(".")
        if not candidate:
            continue
        if candidate.startswith("."):
            if host == candidate[1:] or host.endswith(candidate):
                return True
        elif host == candidate:
            return True
    return False


def check_url(url: str, policy: Policy) -> None:
    """Refuse a URL the policy does not cover. Raises :class:`SafetyError`."""
    parts = urlsplit(url)

    if parts.scheme not in {"http", "https"}:
        raise SafetyError(f"scheme {parts.scheme!r} is not allowed; use http or https ({url})")

    if parts.username or parts.password:
        raise SafetyError(
            "credentials embedded in the URL are not allowed; "
            'reference an environment variable with auth = "env:NAME" instead'
        )

    host = (parts.hostname or "").lower()
    if not host:
        raise SafetyError(f"could not determine a host from {url!r}")

    if not host_allowed(host, policy.allowed_hosts):
        raise SafetyError(
            f"host {host!r} is not in allowed_hosts {policy.allowed_hosts!r}. "
            "Add it to the suite policy on purpose, or fix the base URL."
        )

    if not policy.allow_production:
        for pattern in policy.forbidden_host_patterns:
            if pattern and pattern.lower() in host:
                raise SafetyError(
                    f"host {host!r} matches the production pattern {pattern!r}. "
                    "Re-run with --allow-production if that is genuinely intended."
                )

    if not policy.allow_private_networks and _is_private(host):
        raise SafetyError(
            f"host {host!r} resolves to a private or loopback address. "
            "Set allow_private_networks = true in the suite policy to permit it."
        )


def check_method(method: str, *, mutating: bool, policy: Policy, reads_only: bool = False) -> None:
    """Refuse a write unless both the case and the run opted in.

    ``reads_only`` exists for GraphQL, where every operation is a POST and the
    method therefore says nothing about whether anything changes. The caller
    establishes that from the query document instead; see
    :mod:`parity_gate.graphql`. It is the one place a POST is treated as a
    read, and it is deliberate rather than a loophole: without it every
    GraphQL query would have to be declared a mutation, and a gate everyone
    switches off protects nothing.
    """
    method = method.upper()
    if reads_only or method in SAFE_METHODS:
        return
    if not mutating:
        raise SafetyError(
            f"{method} case is not marked `mutating = true`; "
            "a write must be declared in the suite before it can run"
        )
    if not policy.allow_mutations:
        raise SafetyError(
            f"{method} is a write and mutations are disabled for this run. "
            "Pass --allow-mutations once you are sure the target data is disposable."
        )


#: Put this marker on a line to exempt that line, or anywhere in a file to
#: exempt the whole file. A scanner with no way to say "this one is a fixture"
#: gets a blanket exclusion instead, and then it stops scanning the directory
#: that actually matters.
ALLOW_LINE = "parity-gate:allow-secret"
ALLOW_FILE = "parity-gate:allow-secrets-file"


def scan_for_secrets(text: str, origin: str) -> list[str]:
    """Return human-readable findings for credentials embedded in ``text``.

    This is a guardrail against pasting a live token somewhere it will be
    committed, not a security boundary: whoever writes the file can also write
    the pragma. It is aimed at the accident, which is the realistic case.
    """
    if ALLOW_FILE in text:
        return []

    findings: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_LINE in line:
            continue
        for name in find_secrets(line):
            findings.append(f"{origin}:{number}: looks like a live {name}")
    return findings


def _is_private(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )
