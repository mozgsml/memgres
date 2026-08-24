"""Container liveness probe that knows which role the container is running.

One image, two entrypoints: ``memgres-server`` (REST, ``MEMGRES_HTTP_PORT``,
answers ``/healthz``) and ``memgres-mcp`` (MCP, ``MEMGRES_MCP_PORT``, answers
``/mcp`` and has no ``/healthz``). The old Dockerfile HEALTHCHECK probed a
hardcoded ``localhost:8080/healthz``, so an MCP container was reported unhealthy
forever — it never listens on 8080. A permanently-red healthcheck is worse than
none: it masks real failures and blocks anything waiting on ``service_healthy``.

The role comes from PID 1's argv, not from an env convention, so it stays right
even when a deployment sets MCP variables on a REST container or vice versa.

Two details that make the probe honest rather than merely green:

* **127.0.0.1, not ``localhost``** — a server bound to ``0.0.0.0`` listens on
  IPv4 only, while ``localhost`` may resolve to ``::1`` first and be refused.
* **For MCP, any HTTP answer means healthy.** Streamable HTTP rejects a bare GET
  with 400; that response still proves the port is bound and the app is serving.
  Only a refused connection or a timeout is a real failure.
"""

import os
import sys
import urllib.error
import urllib.request

#: MEMGRES_MCP_TRANSPORT values that make the MCP server listen on a socket.
#: Anything else (i.e. stdio) is spoken over the process's own pipes.
HTTP_TRANSPORTS = ("http", "streamable-http", "streamable_http")

_TIMEOUT = 3


def _pid1_argv() -> list:
    """PID 1's argv — the command the container was actually started with."""
    try:
        with open("/proc/1/cmdline", "rb") as fh:
            return [a for a in fh.read().decode("utf-8", "replace").split("\0") if a]
    except OSError:
        return []                      # not Linux / no procfs — fall back to REST


def role(argv=None) -> str:
    """``"mcp"`` or ``"rest"``. REST is the default, matching the image's CMD."""
    argv = _pid1_argv() if argv is None else argv
    for arg in argv:
        if "memgres-mcp" in arg or "mcp_server" in arg:
            return "mcp"
    return "rest"


def _answers(url: str) -> bool:
    """True if something at `url` speaks HTTP — any status, including errors."""
    try:
        urllib.request.urlopen(url, timeout=_TIMEOUT)
        return True
    except urllib.error.HTTPError:
        return True                    # bound and serving (MCP GET → 400)
    except (urllib.error.URLError, OSError):
        return False                   # refused, unresolved, timed out


def _status_ok(url: str) -> bool:
    """True only on a 200 — used where a real health endpoint exists."""
    try:
        return urllib.request.urlopen(url, timeout=_TIMEOUT).status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return False


def check() -> int:
    """Exit code for the probe: 0 healthy, 1 unhealthy."""
    if role() == "mcp":
        transport = os.environ.get("MEMGRES_MCP_TRANSPORT", "stdio").lower()
        if transport not in HTTP_TRANSPORTS:
            return 0                   # stdio: no socket; docker already sees the process
        port = os.environ.get("MEMGRES_MCP_PORT", "8765")
        return 0 if _answers(f"http://127.0.0.1:{port}/mcp") else 1
    port = os.environ.get("MEMGRES_HTTP_PORT", "8080")
    return 0 if _status_ok(f"http://127.0.0.1:{port}/healthz") else 1


def main():  # pragma: no cover - entrypoint
    sys.exit(check())


if __name__ == "__main__":  # pragma: no cover
    main()
