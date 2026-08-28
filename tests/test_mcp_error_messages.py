"""A refusal the caller cannot read is a refusal that gets repeated.

The server was already answering carefully — "this deployment requires `source`
… as an ADDRESS", "you can reach 2 namespaces, name the one you mean" — and the
caller saw `Error executing tool memory_write` with nothing else. The MCP SDK
masks every exception except its own `ToolError`, so all that text went to the
server log and nowhere else. In production an agent hit the same wall five times
in a row with the same edit, then moved on to reads and hit it again.

So the tools now re-raise domain refusals as `ToolError` — and only those. A
psycopg error or a genuine bug stays masked, because those messages describe our
schema rather than the caller's mistake.

The second half is the one that produced no sentence at all: a `path_prefix`
that is not a path reached Postgres as `%s::ltree` and came back as `ltree
syntax error at character 1`. Validated up front now, in the one place that says
what a path is.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres.paths import check_path, is_path  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


# ─── what a path is, in one place ────────────────────────────────────────────
def test_a_path_is_labels_joined_by_dots():
    assert is_path("ops.memory.onboarding")
    assert is_path("infra.servers.video-production")   # hyphen: ltree allows it
    assert is_path("ops.тариф")                        # any script: so does ltree
    assert check_path("ops.x", "path") == "ops.x"


def test_what_is_not_a_path_is_refused_with_a_sentence():
    for bad in ("not a path", "ops/x", ".leading", "ops..x", "конец "):
        with pytest.raises(ValueError) as e:
            check_path(bad, "path_prefix")
        msg = str(e.value)
        assert "path_prefix" in msg          # which argument
        assert repr(bad) in msg              # what they passed
        assert "joined by dots" in msg       # what it should look like


def test_no_path_is_not_a_bad_path():
    """`None`/empty means "not filtering", and refusing it would break every
    unfiltered list."""
    assert check_path(None, "path_prefix") is None
    assert check_path("", "path_prefix") == ""


# ─── the wrapper: which failures may speak ───────────────────────────────────
def test_domain_refusals_become_the_error_type_the_client_sees():
    from memgres.mcp_server import ToolError, _speaking

    def refuses():
        raise ValueError("say which namespace you mean")

    with pytest.raises(ToolError) as e:
        _speaking(refuses)()
    assert "say which namespace you mean" in str(e.value)


def test_internal_failures_stay_masked():
    """A psycopg error or a bug describes our schema, not their mistake. It must
    NOT be handed to the caller — it stays whatever it was, and the SDK masks it."""
    from memgres.mcp_server import ToolError, _speaking

    def breaks():
        raise RuntimeError("relation \"memory\" does not exist")

    with pytest.raises(RuntimeError):
        _speaking(breaks)()
    try:
        _speaking(breaks)()
    except Exception as e:                    # noqa: BLE001 - asserting the type
        assert not isinstance(e, ToolError)


def test_a_refusal_is_not_wrapped_twice():
    from memgres.mcp_server import ToolError, _speaking

    def refuses():
        raise ToolError("already speakable")

    with pytest.raises(ToolError) as e:
        _speaking(refuses)()
    assert str(e.value) == "already speakable"


# ─── on the wire, which is the only proof that matters ───────────────────────
def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _rpc(url: str, payload: dict, session: str = "") -> tuple:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if session:
        req.add_header("Mcp-Session-Id", session)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode(), r.headers.get("Mcp-Session-Id", "")


@pytest.mark.skipif(not _reachable(), reason="no test Postgres")
def test_every_registered_tool_is_wrapped():
    """The version-independent half of the proof.

    Whether a bare `ValueError` reaches the client is up to the SDK, and it
    CHANGED under us: 2.0.0 put the message into its own error text, 2.1.1 raises
    `UnexpectedToolError("Error executing tool <name>")` and drops it. So the
    wire test below passes on one version whether or not the fix is present. This
    one asserts the fix itself — that what got registered is the wrapper — and
    fails on every SDK generation if the wrapping is removed."""
    os.environ.setdefault("MEMGRES_DATABASE_URL", DSN)
    os.environ["MEMGRES_KEY_MODE"] = "single"
    os.environ["MEMGRES_EMBED_PROVIDER"] = "none"
    from memgres.mcp_server import ToolError, build_server

    srv = build_server()
    mgr = getattr(srv, "_tool_manager", None)
    tools = getattr(mgr, "_tools", None)
    assert tools, "could not reach the registered tools"
    with pytest.raises(ToolError) as e:
        tools["memory_list"].fn(path_prefix="not a path")
    assert "not a tree path" in str(e.value)


@pytest.mark.skipif(not _reachable(), reason="no test Postgres")
def test_the_reason_reaches_the_client_over_http():
    """In-process assertions cannot prove this: the masking happens in the SDK's
    tool layer, on the way out. So this drives a real server over real HTTP and
    reads what a caller would actually be told."""
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
        deadline = time.time() + 30
        session = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"server exited early ({proc.returncode}):\n{out}")
            try:
                _, session = _rpc(url, {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}}})
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.5)
        assert session, "server never completed a handshake"
        _rpc(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)

        text, _ = _rpc(url, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "memory_list",
                       "arguments": {"path_prefix": "not a path"}}}, session)
        # The point of the fix, in one assertion: the sentence, not the mask.
        assert "not a tree path" in text, text[:400]
        assert "joined by dots" in text
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
