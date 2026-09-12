"""A small, deliberately boring HTTP client built on the standard library.

No ``requests``, no ``httpx``: a contract-verification tool should not ask a
reviewer to trust a dependency tree, and every behaviour that matters here
(timeouts, retry policy, redirect handling) is something we want to state
explicitly rather than inherit.

Redirects are re-validated against the safety policy on every hop, because
"the staging host 302s to production" is a real way to lose a database.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from parity_gate import __version__
from parity_gate.safety import Policy, SafetyError, check_url

USER_AGENT = f"parity-gate/{__version__} (+https://github.com/Kattiell/parity-gate)"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class Response:
    """Everything one call produced, including the ways it failed."""

    url: str
    method: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    json_body: Any = None
    json_error: str | None = None
    elapsed_ms: float = 0.0
    attempts: int = 0
    transport_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.transport_error is None and self.status is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "attempts": self.attempts,
            "json_error": self.json_error,
            "transport_error": self.transport_error,
        }


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Applies the same host allowlist to every redirect target."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        check_url(newurl, self._policy)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    """Issues requests under a :class:`~parity_gate.safety.Policy`."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._opener = urllib.request.build_opener(_PolicyRedirectHandler(policy))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> Response:
        """Perform one call, retrying only on transport errors and 5xx/429."""
        check_url(url, self.policy)

        payload: bytes | None = None
        request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if body is not None:
            payload = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})

        response = Response(url=url, method=method.upper())
        deadline_attempts = max(1, self.policy.max_retries + 1)

        for attempt in range(1, deadline_attempts + 1):
            response.attempts = attempt
            started = time.perf_counter()
            try:
                request = urllib.request.Request(  # noqa: S310 - scheme and host validated above
                    url, data=payload, headers=request_headers, method=method.upper()
                )
                with self._opener.open(request, timeout=self.policy.timeout_seconds) as raw:
                    response.status = raw.status
                    response.headers = {k.lower(): v for k, v in raw.headers.items()}
                    raw_body = raw.read()
            except urllib.error.HTTPError as exc:
                # A 4xx/5xx is a result, not a failure: the contract may well
                # say the endpoint answers 404 here.
                response.status = exc.code
                response.headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                raw_body = exc.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                response.elapsed_ms = (time.perf_counter() - started) * 1000
                response.transport_error = f"{type(exc).__name__}: {exc}"
                if attempt < deadline_attempts:
                    time.sleep(_backoff(attempt))
                    continue
                return response
            except SafetyError:
                raise

            response.elapsed_ms = (time.perf_counter() - started) * 1000
            response.transport_error = None
            response.body_text = raw_body.decode("utf-8", errors="replace")
            _decode_json(response)

            if response.status in RETRYABLE_STATUS and attempt < deadline_attempts:
                time.sleep(_backoff(attempt))
                continue
            return response

        return response


def _decode_json(response: Response) -> None:
    if not response.body_text.strip():
        response.json_body = None
        response.json_error = None if response.status in {204, 205, 304} else "empty body"
        return
    try:
        response.json_body = json.loads(response.body_text)
        response.json_error = None
    except json.JSONDecodeError as exc:
        response.json_body = None
        response.json_error = f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}"


def _backoff(attempt: int) -> float:
    """Fixed, short, jitter-free backoff: reproducible runs beat clever ones."""
    return min(0.25 * attempt, 2.0)
