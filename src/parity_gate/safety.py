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
    timeout_seconds: float = 10.0
    max_retries: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed_hosts": self.allowed_hosts,
            "allow_mutations": self.allow_mutations,
            "allow_production": self.allow_production,
            "allow_private_networks": self.allow_private_networks,
            "forbidden_host_patterns": self.forbidden_host_patterns,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
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
            "reference an environment variable with auth = \"env:NAME\" instead"
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


def check_method(method: str, *, mutating: bool, policy: Policy) -> None:
    """Refuse a write unless both the case and the run opted in."""
    method = method.upper()
    if method in SAFE_METHODS:
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


def scan_for_secrets(text: str, origin: str) -> list[str]:
    """Return human-readable findings for credentials embedded in ``text``."""
    return [f"{origin}: looks like a live {name}" for name in find_secrets(text)]


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
