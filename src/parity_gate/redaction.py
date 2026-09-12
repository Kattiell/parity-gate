"""Redaction of secrets and personal data before anything is written to disk.

Evidence files are the whole point of this tool, and evidence files get
attached to tickets, uploaded as CI artifacts and pasted into chat. Every
value that leaves the process passes through :func:`redact` first, so a token
that was legitimately used to make a request never survives into the report.

The rules are intentionally conservative: a false positive costs a reader one
unreadable field, a false negative costs a credential rotation.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"

#: Header names whose value is always replaced, regardless of content.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-access-token",
        "x-csrf-token",
        "api-key",
        "apikey",
        "token",
    }
)

#: JSON keys whose value is always replaced, at any depth.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "accesstoken",
        "refreshtoken",
        "api_key",
        "apikey",
        "client_secret",
        "authorization",
        "private_key",
        "session",
        "sessionid",
        "credit_card",
        "card_number",
        "cvv",
    }
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # JSON Web Token: three base64url segments.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # Bearer/Basic credentials embedded in free text.
    ("http-auth", re.compile(r"\b(?:Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)),
    # Provider-specific tokens with a recognisable prefix.
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # Credentials inside a URL, e.g. postgres://user:hunter2@host/db
    ("url-credentials", re.compile(r"(?<=://)[^/\s:@]{1,64}:[^/\s@]{1,128}(?=@)")),
    # Personal data that shows up constantly in Brazilian test fixtures.
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("cpf", re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")),
    ("cnpj", re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")),
    ("pan", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
)


def redact_text(value: str) -> str:
    """Replace every known secret or personal-data shape found in ``value``."""
    for name, pattern in _PATTERNS:
        if name == "pan" and not _has_card_candidate(value):
            continue
        value = pattern.sub(MASK, value)
    return value


def _has_card_candidate(value: str) -> bool:
    """Cheap pre-filter so the greedy PAN pattern does not scan every string."""
    digits = sum(c.isdigit() for c in value)
    return digits >= 13


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Mask sensitive headers by name and scrub the remaining values by shape."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            out[key] = MASK
        else:
            out[key] = redact_text(str(value))
    return out


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact an arbitrary JSON-like structure.

    Keys are matched case-insensitively and with separators stripped, so
    ``access_token``, ``accessToken`` and ``Access-Token`` are all caught.
    """
    if _depth > 64:  # defensive: refuse to recurse into pathological payloads
        return MASK
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _normalise_key(key) in SENSITIVE_KEYS:
                out[key] = MASK
            else:
                out[key] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _normalise_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def find_secrets(text: str) -> list[str]:
    """Return the names of secret patterns present in ``text``.

    Used by the pre-flight scan to refuse to run a suite that has a live
    credential pasted into it, which is the single most common way a token
    ends up in a public repository.
    """
    hits: list[str] = []
    for name, pattern in _PATTERNS:
        if name in {"email", "cpf", "cnpj", "pan"}:
            continue  # personal data is redacted, but does not block a run
        if pattern.search(text):
            hits.append(name)
    return hits
