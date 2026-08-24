"""The container probe must match the role the container is running.

Regression guard: the Dockerfile HEALTHCHECK used to hit a fixed
``localhost:8080/healthz``, which the MCP entrypoint never serves — every MCP
container sat "unhealthy" forever while serving fine. The last test here is that
exact scenario.

No database needed: these exercise role detection and the probes against a
throwaway HTTP server.
"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from memgres import healthcheck as hc


def _serve(status: int):
    """Run a one-route HTTP server on a free port; yields its port."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                       # noqa: N802 - stdlib naming
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):              # keep pytest output clean
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ─── role detection ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["/usr/local/bin/memgres-mcp"],
    ["python", "-m", "memgres.mcp_server"],
])
def test_role_detects_mcp(argv):
    assert hc.role(argv) == "mcp"


@pytest.mark.parametrize("argv", [
    ["/usr/local/bin/memgres-server"],
    [],                                          # no procfs → default
])
def test_role_defaults_to_rest(argv):
    assert hc.role(argv) == "rest"


# ─── probes ────────────────────────────────────────────────────────────────────

def test_answers_accepts_any_http_status():
    """A bare GET to Streamable HTTP returns 400 — still proof it is serving."""
    srv = _serve(400)
    try:
        assert hc._answers(f"http://127.0.0.1:{srv.server_port}/mcp") is True
    finally:
        srv.shutdown()


def test_answers_false_when_nothing_listens():
    assert hc._answers(f"http://127.0.0.1:{_free_port()}/mcp") is False


def test_status_ok_requires_200():
    srv = _serve(500)
    try:
        assert hc._status_ok(f"http://127.0.0.1:{srv.server_port}/healthz") is False
    finally:
        srv.shutdown()


# ─── check() per role ──────────────────────────────────────────────────────────

def test_mcp_http_healthy_when_port_answers(monkeypatch):
    srv = _serve(400)
    try:
        monkeypatch.setattr(hc, "role", lambda *a: "mcp")
        monkeypatch.setenv("MEMGRES_MCP_TRANSPORT", "http")
        monkeypatch.setenv("MEMGRES_MCP_PORT", str(srv.server_port))
        assert hc.check() == 0
    finally:
        srv.shutdown()


def test_mcp_http_unhealthy_when_dead(monkeypatch):
    monkeypatch.setattr(hc, "role", lambda *a: "mcp")
    monkeypatch.setenv("MEMGRES_MCP_TRANSPORT", "http")
    monkeypatch.setenv("MEMGRES_MCP_PORT", str(_free_port()))
    assert hc.check() == 1


def test_mcp_stdio_is_healthy_without_a_socket(monkeypatch):
    """stdio speaks over pipes; there is no port to probe, so never fail it."""
    monkeypatch.setattr(hc, "role", lambda *a: "mcp")
    monkeypatch.delenv("MEMGRES_MCP_TRANSPORT", raising=False)
    assert hc.check() == 0


def test_rest_healthy_on_200(monkeypatch):
    srv = _serve(200)
    try:
        monkeypatch.setattr(hc, "role", lambda *a: "rest")
        monkeypatch.setenv("MEMGRES_HTTP_PORT", str(srv.server_port))
        assert hc.check() == 0
    finally:
        srv.shutdown()


def test_rest_honours_configured_port(monkeypatch):
    """The old probe hardcoded 8080 and broke on a remapped REST port."""
    srv = _serve(200)
    try:
        monkeypatch.setattr(hc, "role", lambda *a: "rest")
        monkeypatch.setenv("MEMGRES_HTTP_PORT", str(srv.server_port))
        assert hc.check() == 0
        monkeypatch.setenv("MEMGRES_HTTP_PORT", str(_free_port()))
        assert hc.check() == 1
    finally:
        srv.shutdown()


def test_regression_mcp_container_without_8080_is_healthy(monkeypatch):
    """THE BUG: MCP serves on 8765 and nothing listens on 8080 — must be green."""
    srv = _serve(400)
    try:
        monkeypatch.setattr(hc, "role", lambda *a: "mcp")
        monkeypatch.setenv("MEMGRES_MCP_TRANSPORT", "streamable-http")
        monkeypatch.setenv("MEMGRES_MCP_PORT", str(srv.server_port))
        monkeypatch.setenv("MEMGRES_HTTP_PORT", "8080")   # nothing there, as in prod
        assert hc.check() == 0
    finally:
        srv.shutdown()
