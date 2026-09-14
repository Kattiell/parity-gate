"""The HTTP client, tested for the two things that are security behaviour
rather than plumbing: every redirect hop is re-validated, and credentials do
not cross hosts. Connection reuse is proved by counting sockets, not asserted.

parity-gate:allow-secrets-file - the bearer values below are fixtures used to
prove they are stripped, never live credentials.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from parity_gate.httpclient import MAX_JSON_DEPTH, MAX_REDIRECTS, Client
from parity_gate.safety import Policy, SafetyError


class _Server(ThreadingHTTPServer):
    """A programmable server that records what it received."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.connections = 0
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[str, Any] = {}
        self.lock = threading.Lock()

    def process_request(self, request, client_address):  # type: ignore[no-untyped-def]
        with self.lock:
            self.connections += 1
        super().process_request(request, client_address)

    def handle_error(self, request, client_address):  # type: ignore[no-untyped-def]
        # A client that hangs up mid-body is what the size and deadline tests
        # provoke on purpose; anything else is still printed.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        return

    def _handle(self) -> None:
        server: _Server = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        with server.lock:
            server.requests.append(
                {
                    "path": self.path,
                    "method": self.command,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": payload.decode() or None,
                }
            )

        route = server.routes.get(self.path.split("?")[0], {"status": 200, "body": {"ok": True}})
        status = route.get("status", 200)
        headers = route.get("headers", {})
        body = route.get("body")
        raw = route.get("raw")
        if raw is None:
            raw = b"" if body is None else json.dumps(body).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if route.get("undeclared_length"):
            # No Content-Length: the body ends when the connection does.
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(len(raw)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if not raw:
            return
        try:
            if route.get("trickle_seconds"):
                for byte in raw:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(route["trickle_seconds"])
            else:
                self.wfile.write(raw)
        except OSError:
            return  # the client gave up on us, which is what some tests want

    do_GET = do_POST = do_DELETE = _handle


@pytest.fixture
def server() -> Iterator[_Server]:
    instance = _Server()
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()


def client(**overrides: Any) -> Client:
    policy = Policy(allowed_hosts=["127.0.0.1"], allow_private_networks=True, **overrides)
    return Client(policy)


# -- what the rewrite was for ----------------------------------------------


def test_connections_are_reused_across_requests(server: _Server) -> None:
    """The point of the pool. Twelve requests used to mean twelve handshakes."""
    connection = client()
    try:
        for _ in range(12):
            assert connection.request("GET", f"{server.url}/thing").status == 200
    finally:
        connection.close()

    assert len(server.requests) == 12
    assert server.connections == 1, f"expected one socket, server accepted {server.connections}"


def test_a_dead_pooled_connection_is_recovered_for_an_idempotent_method(
    server: _Server,
) -> None:
    connection = client()
    try:
        connection.request("GET", f"{server.url}/thing")
        # Simulate the peer having closed it while we were not looking.
        for pooled in connection.pool._connections.values():
            pooled.sock.close()  # type: ignore[union-attr]
        assert connection.request("GET", f"{server.url}/thing").status == 200
    finally:
        connection.close()


def test_closing_the_client_releases_every_connection(server: _Server) -> None:
    connection = client()
    connection.request("GET", f"{server.url}/thing")
    assert connection.pool.size == 1
    connection.close()
    assert connection.pool.size == 0


# -- redirects: the security half -------------------------------------------


def test_a_redirect_is_followed_and_recorded(server: _Server) -> None:
    server.routes["/start"] = {"status": 302, "headers": {"Location": "/end"}}
    server.routes["/end"] = {"status": 200, "body": {"arrived": True}}

    connection = client()
    try:
        response = connection.request("GET", f"{server.url}/start")
    finally:
        connection.close()

    assert response.status == 200
    assert response.json_body == {"arrived": True}
    assert response.redirects == [f"{server.url}/end"]


def test_a_redirect_off_the_allow_list_is_refused_before_the_next_request(
    server: _Server,
) -> None:
    """The staging host that 302s to production. The check has to happen before
    the packet, which is why this raises instead of returning a response."""
    server.routes["/start"] = {
        "status": 302,
        "headers": {"Location": "https://api.prod.example.com/x"},
    }
    connection = client()
    try:
        with pytest.raises(SafetyError, match="not in allowed_hosts"):
            connection.request("GET", f"{server.url}/start")
    finally:
        connection.close()
    # One request made, none to the redirect target.
    assert [r["path"] for r in server.requests] == ["/start"]


def test_credentials_are_not_replayed_across_a_host_change(server: _Server) -> None:
    """A token issued for one host has no business reaching another, even when
    that host is also on the allow-list."""
    other = _Server()
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        server.routes["/start"] = {"status": 302, "headers": {"Location": f"{other.url}/end"}}
        other.routes["/end"] = {"status": 200, "body": {"ok": True}}

        connection = client()
        try:
            connection.request(
                "GET",
                f"{server.url}/start",
                headers={"Authorization": "Bearer s3cret0000", "X-Trace": "keep-me"},
            )
        finally:
            connection.close()

        forwarded = other.requests[0]["headers"]
        assert "authorization" not in forwarded
        assert forwarded["x-trace"] == "keep-me"  # ordinary headers survive
    finally:
        other.shutdown()
        other.server_close()


def test_credentials_survive_a_redirect_within_the_same_host(server: _Server) -> None:
    server.routes["/start"] = {"status": 302, "headers": {"Location": "/end"}}
    connection = client()
    try:
        connection.request(
            "GET", f"{server.url}/start", headers={"Authorization": "Bearer s3cret0000"}
        )
    finally:
        connection.close()
    assert server.requests[1]["headers"]["authorization"] == "Bearer s3cret0000"


def test_303_degrades_a_write_to_a_get(server: _Server) -> None:
    server.routes["/submit"] = {"status": 303, "headers": {"Location": "/result"}}
    connection = client(allow_mutations=True)
    try:
        connection.request("POST", f"{server.url}/submit", body={"a": 1})
    finally:
        connection.close()

    assert server.requests[0]["method"] == "POST"
    assert server.requests[1]["method"] == "GET"
    assert server.requests[1]["body"] is None


def test_307_preserves_the_method_and_the_body(server: _Server) -> None:
    server.routes["/submit"] = {"status": 307, "headers": {"Location": "/elsewhere"}}
    connection = client(allow_mutations=True)
    try:
        connection.request("POST", f"{server.url}/submit", body={"a": 1})
    finally:
        connection.close()

    assert [r["method"] for r in server.requests] == ["POST", "POST"]
    assert json.loads(server.requests[1]["body"]) == {"a": 1}


def test_a_redirect_loop_stops_instead_of_spinning(server: _Server) -> None:
    server.routes["/loop"] = {"status": 302, "headers": {"Location": "/loop"}}
    connection = client()
    try:
        response = connection.request("GET", f"{server.url}/loop")
    finally:
        connection.close()

    assert "stopped after" in (response.json_error or "")
    assert len(server.requests) == MAX_REDIRECTS + 1


# -- ordinary behaviour ------------------------------------------------------


def test_an_error_status_is_a_result_not_an_exception(server: _Server) -> None:
    server.routes["/gone"] = {"status": 410, "body": {"error": "gone"}}
    connection = client()
    try:
        response = connection.request("GET", f"{server.url}/gone")
    finally:
        connection.close()

    assert response.status == 410
    assert response.json_body == {"error": "gone"}
    assert response.transport_error is None


def test_retries_apply_only_to_the_statuses_that_deserve_them(server: _Server) -> None:
    server.routes["/flaky"] = {"status": 503, "body": {"busy": True}}
    connection = client(max_retries=2)
    try:
        response = connection.request("GET", f"{server.url}/flaky")
    finally:
        connection.close()

    assert response.status == 503
    assert response.attempts == 3
    assert len(server.requests) == 3


def test_retries_are_off_unless_asked_for(server: _Server) -> None:
    server.routes["/flaky"] = {"status": 503, "body": {"busy": True}}
    connection = client()
    try:
        assert connection.request("GET", f"{server.url}/flaky").attempts == 1
    finally:
        connection.close()
    assert len(server.requests) == 1


def test_an_unreachable_host_becomes_a_transport_error(server: _Server) -> None:
    connection = client()
    try:
        response = connection.request("GET", "http://127.0.0.1:1/nothing")
    finally:
        connection.close()
    assert response.status is None
    assert response.transport_error


def test_a_url_the_policy_forbids_never_leaves(server: _Server) -> None:
    connection = client()
    try:
        with pytest.raises(SafetyError):
            connection.request("GET", "https://api.example.com/x")
    finally:
        connection.close()
    assert server.requests == []


def test_a_query_string_reaches_the_server_intact(server: _Server) -> None:
    connection = client()
    try:
        connection.request("GET", f"{server.url}/search?q=phone&limit=5")
    finally:
        connection.close()
    assert server.requests[0]["path"] == "/search?q=phone&limit=5"


# -- a misbehaving service must not take the gate down with it ----------------


def test_a_trickled_body_hits_the_deadline_instead_of_holding_the_run_open(
    server: _Server,
) -> None:
    """A read timeout alone is reset by every byte. At one byte per 50 ms a
    one-second timeout never fires, and this exchange would take four seconds;
    a slower trickle would take as long as the server liked."""
    server.routes["/slow"] = {"raw": b'{"padding": "' + b"x" * 64 + b'"}', "trickle_seconds": 0.05}
    connection = client(timeout_seconds=1.0)
    started = time.monotonic()
    try:
        response = connection.request("GET", f"{server.url}/slow")
    finally:
        connection.close()

    assert time.monotonic() - started < 2.5
    assert response.status is None
    assert "DeadlineExceeded" in (response.transport_error or "")


def test_a_body_that_declares_itself_too_large_is_refused_before_it_is_read(
    server: _Server,
) -> None:
    server.routes["/export"] = {"raw": b"[" + b"1," * 600 + b"1]"}
    connection = client(max_response_bytes=500)
    try:
        response = connection.request("GET", f"{server.url}/export")
    finally:
        connection.close()

    assert response.json_body is None
    assert "ResponseTooLarge" in (response.transport_error or "")
    assert "declares" in (response.transport_error or "")


def test_a_body_without_a_declared_length_is_cut_off_at_the_cap(server: _Server) -> None:
    server.routes["/stream"] = {"raw": b"[" + b"1," * 600 + b"1]", "undeclared_length": True}
    connection = client(max_response_bytes=500)
    try:
        response = connection.request("GET", f"{server.url}/stream")
    finally:
        connection.close()

    assert "exceeded max_response_bytes" in (response.transport_error or "")


def test_an_oversized_body_on_a_pooled_connection_is_not_retried(server: _Server) -> None:
    """The stale-socket retry exists for connections the peer closed. A body
    that is simply too big would be exactly as big the second time."""
    server.routes["/export"] = {"raw": b"[" + b"1," * 600 + b"1]"}
    connection = client(max_response_bytes=500)
    try:
        connection.request("GET", f"{server.url}/thing")  # pools the connection
        connection.request("GET", f"{server.url}/export")
    finally:
        connection.close()

    assert [r["path"] for r in server.requests] == ["/thing", "/export"]


def test_a_body_below_the_cap_still_comes_back_whole(server: _Server) -> None:
    payload = {"items": list(range(200))}
    server.routes["/list"] = {"body": payload}
    connection = client(max_response_bytes=len(json.dumps(payload)))
    try:
        response = connection.request("GET", f"{server.url}/list")
    finally:
        connection.close()

    assert response.transport_error is None
    assert response.json_body == payload


def test_json_nested_past_what_the_parser_survives_is_a_finding_not_a_crash(
    server: _Server,
) -> None:
    """Five thousand levels make `json.loads` raise RecursionError, which used
    to escape the worker thread and end the run without writing evidence."""
    server.routes["/deep"] = {"raw": b"[" * 5000 + b"]" * 5000}
    connection = client()
    try:
        response = connection.request("GET", f"{server.url}/deep")
    finally:
        connection.close()

    assert response.status == 200
    assert response.json_body is None
    assert "nested" in (response.json_error or "")


def test_json_nesting_is_accepted_up_to_the_limit_and_refused_past_it(server: _Server) -> None:
    server.routes["/at-limit"] = {"raw": b"[" * MAX_JSON_DEPTH + b"]" * MAX_JSON_DEPTH}
    server.routes["/past-limit"] = {
        "raw": b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1)
    }
    connection = client()
    try:
        at_limit = connection.request("GET", f"{server.url}/at-limit")
        past_limit = connection.request("GET", f"{server.url}/past-limit")
    finally:
        connection.close()

    assert at_limit.json_error is None and at_limit.json_body is not None
    assert past_limit.json_body is None
    assert f"more than {MAX_JSON_DEPTH} levels" in (past_limit.json_error or "")
