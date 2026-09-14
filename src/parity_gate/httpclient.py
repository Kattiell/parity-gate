"""A small, deliberately boring HTTP client built on the standard library.

No ``requests``, no ``httpx``: a contract-verification tool should not ask a
reviewer to trust a dependency tree, and every behaviour that matters here
(timeouts, retry policy, redirect handling, connection reuse) is something we
want to state explicitly rather than inherit.

This sits on :mod:`http.client` rather than :mod:`urllib.request` for one
reason: ``urllib`` closes the socket after every response, and a suite of two
hundred endpoints sampled three times against two targets pays for twelve
hundred TCP and TLS handshakes. Connections are pooled per host and reused.

Two properties are worth reading the code for, because they are security
behaviour rather than plumbing:

* **Every redirect hop is re-validated** against the safety policy. "The
  staging host 302s to production" is a real way to lose a database, and the
  check happens before the next request is sent, not after.
* **Credentials are dropped when a redirect crosses hosts.** A token issued for
  ``api.staging`` has no business being replayed to whatever answered the
  redirect, even when that host is also on the allow-list.

And two that keep a misbehaving service from taking the gate down with it:

* **A response is bounded in time and in size.** A socket timeout alone is
  reset by every byte that arrives, so a server trickling one byte a second
  would hold a CI job open indefinitely. ``timeout_seconds`` is therefore a
  deadline for the whole exchange, and ``max_response_bytes`` caps the body.
* **A body nested deeper than the tool can walk is refused at decode time.**
  Everything downstream (inference, diffing, redaction) recurses, so the limit
  is enforced once, here, as a finding about the response rather than as a
  ``RecursionError`` about the tool.

A :class:`Client` owns its connections and is **not thread-safe**. The runner
keeps one per worker thread; nothing else should share one.
"""

from __future__ import annotations

import http.client
import json
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from parity_gate import __version__
from parity_gate.safety import SAFE_METHODS, Policy, check_url

USER_AGENT = f"parity-gate/{__version__} (+https://github.com/Kattiell/parity-gate)"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Statuses that carry the request onward, and what they do to the method.
#: 301/302/303 degrade a write to a GET (RFC 9110 §15.4); 307/308 do not.
REDIRECT_TO_GET = frozenset({301, 302, 303})
REDIRECT_PRESERVING = frozenset({307, 308})
REDIRECT_STATUS = REDIRECT_TO_GET | REDIRECT_PRESERVING

MAX_REDIRECTS = 5

#: Containers nested inside one another before a body is refused. The same
#: bound redaction applies, so nothing reaches the evidence that redaction could
#: not have walked. Real APIs sit far below it; a payload above it is either
#: broken or hostile, and neither should crash the run.
MAX_JSON_DEPTH = 64

_READ_CHUNK = 64 * 1024

#: Never replayed to a host other than the one they were sent to.
CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})


class ResponseTooLarge(OSError):
    """The body exceeded ``policy.max_response_bytes``. Never retried."""


class DeadlineExceeded(TimeoutError):
    """The exchange did not complete within ``policy.timeout_seconds``. Never retried."""


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
    #: How many redirects were followed to get here, and where it ended up.
    redirects: list[str] = field(default_factory=list)

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
            "redirects": self.redirects,
            "json_error": self.json_error,
            "transport_error": self.transport_error,
        }


class ConnectionPool:
    """One live connection per ``(scheme, host, port)``, reused until it dies."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self._connections: dict[tuple[str, str, int], http.client.HTTPConnection] = {}
        self._context = ssl.create_default_context()

    def get(self, scheme: str, host: str, port: int) -> http.client.HTTPConnection:
        key = (scheme, host, port)
        existing = self._connections.get(key)
        if existing is not None:
            return existing
        if scheme == "https":
            created: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=self.timeout, context=self._context
            )
        else:
            created = http.client.HTTPConnection(host, port, timeout=self.timeout)
        self._connections[key] = created
        return created

    def holds(self, scheme: str, host: str, port: int) -> bool:
        """Whether a connection to this host is already open and pooled."""
        return (scheme, host, port) in self._connections

    def drop(self, scheme: str, host: str, port: int) -> None:
        """Forget a connection, closing it if it is still open."""
        connection = self._connections.pop((scheme, host, port), None)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        for key in list(self._connections):
            self.drop(*key)

    @property
    def size(self) -> int:
        return len(self._connections)


class Client:
    """Issues requests under a :class:`~parity_gate.safety.Policy`."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.pool = ConnectionPool(policy.timeout_seconds)

    def close(self) -> None:
        self.pool.close()

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
        attempts_allowed = max(1, self.policy.max_retries + 1)

        for attempt in range(1, attempts_allowed + 1):
            response.attempts = attempt
            started = time.perf_counter()
            try:
                self._follow(response, method.upper(), url, dict(request_headers), payload)
            except OSError as exc:
                response.elapsed_ms = (time.perf_counter() - started) * 1000
                response.transport_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts_allowed:
                    time.sleep(_backoff(attempt))
                    continue
                return response

            response.elapsed_ms = (time.perf_counter() - started) * 1000
            response.transport_error = None

            if response.status in RETRYABLE_STATUS and attempt < attempts_allowed:
                time.sleep(_backoff(attempt))
                continue
            return response

        return response

    # -- redirects ----------------------------------------------------------

    def _follow(
        self,
        response: Response,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: bytes | None,
    ) -> None:
        """Send the request, following redirects and re-checking every hop."""
        current = url
        response.redirects = []

        for _ in range(MAX_REDIRECTS + 1):
            status, received, raw = self._send(method, current, headers, payload)
            response.status = status
            response.headers = received
            location = received.get("location")

            if status not in REDIRECT_STATUS or not location:
                response.body_text = raw.decode("utf-8", errors="replace")
                _decode_json(response)
                return

            target = urljoin(current, location)
            # Before the next packet, not after: this is the control that keeps
            # a staging suite from being walked into production.
            check_url(target, self.policy)

            if _different_host(current, target):
                headers = {
                    name: value
                    for name, value in headers.items()
                    if name.lower() not in CREDENTIAL_HEADERS
                }
            if status in REDIRECT_TO_GET and method not in {"GET", "HEAD"}:
                method, payload = "GET", None
                headers.pop("Content-Type", None)

            response.redirects.append(target)
            current = target

        response.body_text = ""
        response.json_body = None
        response.json_error = f"stopped after {MAX_REDIRECTS} redirects"

    # -- one hop ------------------------------------------------------------

    def _send(
        self, method: str, url: str, headers: dict[str, str], payload: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        """One request over a pooled connection, recovering from a dead socket."""
        parts = urlsplit(url)
        scheme = parts.scheme
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        target = urlunsplit(("", "", parts.path or "/", parts.query, ""))

        # A pooled connection the peer has since closed fails on the way out.
        # The useful question is not which exception that produced (the answer
        # differs by platform and by how the peer went away) but whether this
        # socket was one we had just opened. A connection we did not open may be
        # stale, so one retry on a fresh one is worth it; a brand new connection
        # that failed will fail again. Retrying is still limited to methods that
        # can be repeated without changing anything twice.
        allowed_tries = 2 if method in SAFE_METHODS else 1

        for attempt in range(allowed_tries):
            reused = self.pool.holds(scheme, host, port)
            connection = self.pool.get(scheme, host, port)
            deadline = time.monotonic() + self.policy.timeout_seconds
            try:
                connection.request(method, target, body=payload, headers=headers)
                raw_response = connection.getresponse()
                body = self._read_body(raw_response, connection, deadline)
            except (TimeoutError, http.client.HTTPException, OSError) as exc:
                self.pool.drop(scheme, host, port)
                # A body that was too big or too slow says nothing about the
                # socket being stale; a second try would only double the cost.
                bounded = isinstance(exc, ResponseTooLarge | DeadlineExceeded)
                if reused and not bounded and attempt + 1 < allowed_tries:
                    continue
                raise

            received = {k.lower(): v for k, v in raw_response.getheaders()}
            if _closing(raw_response, received):
                self.pool.drop(scheme, host, port)
            return raw_response.status, received, body

        raise OSError("connection could not be established")  # pragma: no cover

    def _read_body(
        self,
        raw_response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        deadline: float,
    ) -> bytes:
        """Read the body under a size cap and a deadline for the whole exchange.

        ``read1`` returns after at most one read on the socket, and the socket
        timeout is shrunk to whatever is left of the deadline before each one.
        Together that makes the deadline real: a server sending one byte a
        second cannot keep resetting it.

        The status line and headers are read by :mod:`http.client` itself, so
        they are bounded by the socket timeout per read and by its own header
        limits rather than by this deadline. A server that trickles *headers*
        can still stretch an exchange; bodies are where runaway responses live.
        """
        limit = self.policy.max_response_bytes
        declared = raw_response.getheader("Content-Length") or ""
        if declared.isdigit() and int(declared) > limit:
            raise ResponseTooLarge(
                f"response declares {declared} bytes, above max_response_bytes ({limit})"
            )

        chunks: list[bytes] = []
        received = 0

        def overdue() -> DeadlineExceeded:
            return DeadlineExceeded(
                f"response not complete within {self.policy.timeout_seconds}s "
                f"({received} bytes received)"
            )

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise overdue()
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                try:
                    chunk = raw_response.read1(_READ_CHUNK)
                except TimeoutError as exc:
                    # The socket timeout was the remainder of the deadline, so
                    # a timeout here is the deadline, whatever class it has.
                    raise overdue() from exc
                if not chunk:
                    return b"".join(chunks)
                received += len(chunk)
                if received > limit:
                    raise ResponseTooLarge(
                        f"response exceeded max_response_bytes ({limit}) before it ended"
                    )
                chunks.append(chunk)
        finally:
            # The connection goes back into the pool; the next request is owed
            # the full timeout, not the remainder of this one.
            if connection.sock is not None:
                connection.sock.settimeout(self.policy.timeout_seconds)


def _closing(raw_response: http.client.HTTPResponse, headers: dict[str, str]) -> bool:
    """Whether this connection must not go back into the pool."""
    if headers.get("connection", "").lower() == "close":
        return True
    return raw_response.version < 11 and "keep-alive" not in headers.get("connection", "").lower()


def _different_host(current: str, target: str) -> bool:
    left, right = urlsplit(current), urlsplit(target)
    return (left.scheme, left.hostname, left.port) != (right.scheme, right.hostname, right.port)


def _decode_json(response: Response) -> None:
    if not response.body_text.strip():
        response.json_body = None
        response.json_error = None if response.status in {204, 205, 304} else "empty body"
        return
    try:
        decoded = json.loads(response.body_text)
    except json.JSONDecodeError as exc:
        response.json_body = None
        response.json_error = f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}"
        return
    except RecursionError:
        # The parser itself gave up. Caught here, because anything that escapes
        # a worker thread takes the whole run down without writing evidence.
        response.json_body = None
        response.json_error = f"JSON nested too deeply to parse (limit {MAX_JSON_DEPTH} levels)"
        return

    if _nesting_exceeds(decoded, MAX_JSON_DEPTH):
        response.json_body = None
        response.json_error = (
            f"JSON nested more than {MAX_JSON_DEPTH} levels deep; refused rather than walked"
        )
        return
    response.json_body = decoded
    response.json_error = None


def _nesting_exceeds(value: Any, limit: int) -> bool:
    """Whether containers nest more than ``limit`` deep. Iterative on purpose."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            children: Any = current.values()
        elif isinstance(current, list):
            children = current
        else:
            continue
        if depth + 1 > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _backoff(attempt: int) -> float:
    """Fixed, short, jitter-free backoff: reproducible runs beat clever ones."""
    return min(0.25 * attempt, 2.0)
