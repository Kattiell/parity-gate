"""Redaction of secrets and personal data before anything is written to disk.

Evidence files are the whole point of this tool, and evidence files get
attached to tickets, uploaded as CI artifacts and pasted into chat. Every
value that leaves the process passes through :func:`redact` first, so a token
that was legitimately used to make a request never survives into the report.

The rules match **known shapes**: sensitive header and key names at any depth,
and token formats with a recognisable prefix. That boundary is real and worth
stating plainly — a bare 32-character API key, an AWS *secret* access key or an
opaque session id carries no marker, and nothing here will catch it when it
appears as a naked value under an innocuous key. Put credentials behind the key
names in :data:`SENSITIVE_KEYS`, or review evidence before publishing it.

Two failure directions, weighted differently: a missed secret costs a
credential rotation, so prefixed shapes are matched aggressively. A false
positive costs a reader an unreadable field — which is why the one rule that
cannot be anchored to a prefix, card numbers, is gated behind a Luhn check
rather than matching every long run of digits.


parity-gate:allow-secrets-file - every credential-shaped string below is a
fixture or a pattern definition, never a live value.
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
    # The value has to look like a credential, not merely follow the word.
    # Matching anything 8 characters long after "Token" flagged the English
    # sentence "a token supplied through the environment" in this project's own
    # README — and a scanner that flags its own documentation earns a blanket
    # exclusion, after which it stops scanning the code that matters. A digit,
    # or a long unbroken run of letters, is the cheap discriminator.
    (
        "http-auth",
        re.compile(
            r"\b(?:Bearer|Basic|Token)\s+"
            r"(?:(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{8,}|[A-Za-z]{16,})",
            re.IGNORECASE,
        ),
    ),
    # Provider-specific tokens with a recognisable prefix.
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("stripe-key", re.compile(r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("digitalocean-token", re.compile(r"\bdop_v1_[a-f0-9]{32,}\b")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # Credentials inside a URL, e.g. postgres://user:hunter2@host/db
    ("url-credentials", re.compile(r"(?<=://)[^/\s:@]{1,64}:[^/\s@]{1,128}(?=@)")),
    # Personal data that shows up constantly in Brazilian test fixtures.
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("cpf", re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")),
    ("cnpj", re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")),
    # Card numbers are checked with Luhn before being masked. Without that, a
    # 13-digit EAN barcode, a phone number with a country code and a nanosecond
    # timestamp all get destroyed in the evidence, while a 23-digit order
    # number sails through because it is one digit past the upper bound. An
    # arbitrary rule that mangles real data is worse than no rule: the evidence
    # stops being usable in the ticket it was collected for.
    ("pan", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
)


def redact_text(value: str) -> str:
    """Replace every known secret or personal-data shape found in ``value``."""
    for name, pattern in _PATTERNS:
        if name == "pan":
            if _has_card_candidate(value):
                value = pattern.sub(_mask_if_card, value)
            continue
        value = pattern.sub(MASK, value)
    return value


def _has_card_candidate(value: str) -> bool:
    """Cheap pre-filter so the greedy PAN pattern does not scan every string."""
    digits = sum(c.isdigit() for c in value)
    return digits >= 13


def _mask_if_card(match: re.Match[str]) -> str:
    """Mask a digit run only when it passes the Luhn checksum.

    Every payment card number satisfies Luhn; almost nothing else does, so this
    is the cheap test that separates a real PAN from a barcode, an order id or
    a timestamp. It is not proof — roughly one in ten random digit strings
    passes by chance — but it turns a rule that destroyed legitimate data into
    one that rarely does, at no cost to the numbers actually worth hiding.
    """
    raw = match.group(0)
    return MASK if luhn(raw) else raw


def luhn(value: str) -> bool:
    """True when the digits in ``value`` satisfy the Luhn checksum."""
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


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
            if _normalise_key(key) in SENSITIVE_KEYS and item not in (None, "", [], {}):
                # An empty value under a sensitive key holds no secret, and
                # masking it destroys the one thing the reader needs: whether
                # the credential was sent at all. "[REDACTED]" where the API
                # returned null is a lie that costs a debugging session.
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
