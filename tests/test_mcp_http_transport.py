"""The http transport must actually bind its port.

Regression guard for the mcp SDK 2.x drift: `main()` used to set
`server.settings.host`, which 2.x's MCPServer rejects ("Settings object has no
field host"), so the server crash-looped and never served. This spawns the real
entrypoint over HTTP and asserts the port answers.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_http_transport_binds_and_answers():
    port = _free_port()
    env = {**os.environ,
           "MEMGRES_DATABASE_URL": DSN,
           "MEMGRES_KEY_MODE": "single",
           "MEMGRES_EMBED_PROVIDER": "none",
           "MEMGRES_MCP_TRANSPORT": "http",
           "MEMGRES_MCP_HOST": "127.0.0.1",
           "MEMGRES_MCP_PORT": str(port)}
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen([sys.executable, "-m", "memgres.mcp_server"],
                            env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.time() + 25
        answered = False
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"server exited early (code {proc.returncode}):\n{out}")
            try:
                urllib.request.urlopen(url, timeout=2)
                answered = True            # 2xx (unlikely for a bare GET)
                break
            except urllib.error.HTTPError:
                answered = True            # bound and speaking HTTP (e.g. 400/406)
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.5)            # not up yet — retry
        assert answered, f"{url} never answered — the transport did not bind"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
