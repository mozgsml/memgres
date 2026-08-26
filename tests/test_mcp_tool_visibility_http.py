"""Tool visibility over the HTTP transport — with the credential in a HEADER.

This file exists because its absence shipped a bug. Every other visibility test
supplies the caller through `MEMGRES_TOKEN`, the env pin — the one path that
resolves a token WITHOUT the request context. The header path, which is the only
way an HTTP deployment identifies anyone, was never exercised, so nothing noticed
that on mcp 2.x the filter had no way to reach the request at all: `MCPServer` has
no `get_context()`, the failure was swallowed into "caller unknown", and every
client of a managed HTTP endpoint — superadmin included — was handed the read-only
subset. The suite was green the whole time.

So these tests speak the wire protocol to a REAL server in a subprocess: initialize,
notifications/initialized, tools/list, with whatever headers the scenario is about.
Slower than the rest of the suite, and the cost is the point — nothing between the
test and the client's own path is stubbed.
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
pytest.importorskip("psycopg_pool")
pytest.importorskip("mcp")

from memgres import identity  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")

# No proxy for these: the developer environment may export HTTP_PROXY, and a
# request to 127.0.0.1 that goes out through a proxy fails in a way that looks
# like the server never started.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """A real `memgres-mcp` on the HTTP transport, talked to over the wire."""

    def __init__(self, **env_extra):
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMGRES_")}
        env.update(MEMGRES_DATABASE_URL=DSN, MEMGRES_EMBED_PROVIDER="none",
                   MEMGRES_FTS_LANGUAGE="simple", MEMGRES_REQUIRE_TITLE="false",
                   MEMGRES_MCP_TRANSPORT="http", MEMGRES_MCP_HOST="127.0.0.1",
                   MEMGRES_MCP_PORT=str(self.port))
        env.update(env_extra)
        self.proc = subprocess.Popen([sys.executable, "-m", "memgres.mcp_server"],
                                     env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._wait()

    def _wait(self, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited:\n" + (self.proc.stdout.read() or ""))
            try:
                _OPENER.open(self.url, timeout=1)
            except urllib.error.HTTPError:
                return                      # answering HTTP at all is enough
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("server never came up")

    def tools(self, token=None, header="authorization"):
        """The tool names a client with these headers actually receives."""
        headers = {}
        if token is not None:
            headers[header] = ("Bearer " + token if header == "authorization"
                               else token)
        sid = self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18",
                                    "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "1"}}},
                        headers)[0]
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized",
                   "params": {}}, headers, sid)
        res = self._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                         "params": {}}, headers, sid)[1]
        return sorted(t["name"] for t in res["result"]["tools"])

    def call(self, name, args, token=None):
        headers = {"authorization": "Bearer " + token} if token else {}
        sid = self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18",
                                    "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "1"}}},
                        headers)[0]
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized",
                   "params": {}}, headers, sid)
        return self._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": name, "arguments": args}},
                         headers, sid)[1]

    def _rpc(self, payload, headers, sid=None):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", **headers}
        if sid:
            h["Mcp-Session-Id"] = sid
        req = urllib.request.Request(self.url, json.dumps(payload).encode(), h)
        resp = _OPENER.open(req, timeout=30)
        body = resp.read().decode()
        if "data:" in body:                                   # SSE framing
            body = "".join(l[6:] for l in body.splitlines() if l.startswith("data: "))
        return resp.headers.get("Mcp-Session-Id"), (json.loads(body) if body.strip() else None)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def _fresh_db():
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()")
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


@pytest.fixture(scope="module")
def managed():
    """One managed endpoint plus a credential for every tier that exists.

    Module-scoped on purpose: the scenarios differ only in the HEADER they send,
    so they can share a server — and the credentials must be minted before it
    starts, because the schema has to exist for the token rows to live in.
    """
    _fresh_db()
    root = identity.new_token()
    boot = Server(MEMGRES_KEY_MODE="managed", MEMGRES_ADMIN_TOKEN=root,
                  MEMGRES_ADMIN_ROLE="superadmin")     # migrates + seeds root
    boot.stop()

    conn = psycopg.connect(DSN, autocommit=True)
    uid = identity.create_user(conn, name="worker")
    nsid = identity.create_namespace(conn, uid, "work")
    writer, _ = identity.issue_token(conn, uid, permission="write")
    reader, _ = identity.issue_token(conn, uid, permission="read")
    scoped, _ = identity.issue_token(conn, uid, namespace_id=nsid,
                                     permission="write")
    muid = identity.create_user(conn, name="manager")
    identity.set_role(conn, muid, "user_manager")
    manager, _ = identity.issue_token(conn, muid, permission="admin")

    srv = Server(MEMGRES_KEY_MODE="managed", MEMGRES_ADMIN_TOKEN=root,
                 MEMGRES_ADMIN_ROLE="superadmin")
    yield srv, {"root": root, "writer": writer, "reader": reader,
                "scoped": scoped, "manager": manager}
    srv.stop()
    conn.close()


def _admin(names):
    return [n for n in names if n.startswith("memory_admin_")]


# ─── the credential in the header decides, and it is actually read ───────────
def test_a_superadmin_header_is_shown_the_control_plane(managed):
    """The regression this file was written for. On a managed HTTP endpoint the
    filter could not reach the request, so this list came back read-only."""
    srv, tok = managed
    names = srv.tools(tok["root"])
    assert "memory_write" in names
    assert "memory_admin_create_namespace" in names
    assert "memory_admin_set_role" in names          # deployment-wide tier


def test_the_header_changes_the_answer(managed):
    """Two callers, one endpoint, different lists — the property that proves the
    header is read rather than ignored. Identical lists were the bug."""
    srv, tok = managed
    assert srv.tools(tok["root"]) != srv.tools(tok["reader"])


def test_a_reader_is_shown_no_write_tools(managed):
    srv, tok = managed
    names = srv.tools(tok["reader"])
    assert "memory_recall" in names and "memory_get" in names
    assert "memory_write" not in names
    assert "memory_move" not in names and "memory_forget" not in names


def test_a_writer_is_shown_no_control_plane(managed):
    srv, tok = managed
    names = srv.tools(tok["writer"])
    assert "memory_write" in names
    assert _admin(names) == []


def test_a_user_manager_gets_the_user_tier_but_not_the_deployment_tier(managed):
    srv, tok = managed
    names = srv.tools(tok["manager"])
    assert "memory_admin_create_user" in names
    assert "memory_admin_create_namespace" in names
    assert "memory_admin_set_role" not in names        # deployment-wide
    assert "memory_admin_adopt_orphans" not in names


def test_a_scoped_credential_is_narrowed_even_though_its_owner_is_not(managed):
    """Rights are the token's, not the person's: a namespace-scoped token cannot
    create another namespace even though its user may."""
    srv, tok = managed
    assert "memory_create_space" not in srv.tools(tok["scoped"])


def test_the_other_header_spelling_works_too(managed):
    """`X-Memgres-Token` is documented alongside `Authorization: Bearer`. A
    documented way in that quietly yields the anonymous list is the same failure
    in a second costume."""
    srv, tok = managed
    assert srv.tools(tok["root"], header="x-memgres-token") == srv.tools(tok["root"])


# ─── an unusable credential must not look like a poor one ────────────────────
def test_no_header_at_all_gets_the_read_surface(managed):
    srv, _ = managed
    names = srv.tools(None)
    assert "memory_recall" in names                  # a working server, not a wall
    assert "memory_write" not in names and _admin(names) == []


def test_a_garbage_token_gets_the_same_read_surface(managed):
    srv, _ = managed
    assert srv.tools("mgk_not_a_real_token") == srv.tools(None)


def test_hiding_is_not_how_a_tool_is_refused(managed):
    """A hidden tool called anyway must still FAIL, and must not come back as if
    it did not exist. Hiding is a display economy; the refusal is the call path's
    job, and if a rights problem surfaced as "unknown tool" it would read as a
    broken server while the real check sat in a display table.

    Only the failure and the absence of a not-found story are asserted. Whether
    the reason survives into the client's view is the SDK's rendering, and newer
    mcp releases replace a tool's exception with a generic "error executing
    tool" — an argument for reading the server log, not for asserting wording
    this project does not own."""
    srv, tok = managed
    res = srv.call("memory_write", {"body": "x", "path": "a.b", "title": "T"},
                   token=tok["reader"])
    text = json.dumps(res).lower()
    assert res["result"]["isError"] is True or res["result"].get("iserror") is True
    for missing in ("unknown tool", "not found", "no such tool"):
        assert missing not in text


# ─── the other deployment shapes ─────────────────────────────────────────────
def test_a_single_tenant_endpoint_hides_identity_and_shows_the_rest():
    """`single` has one implicit caller who may do everything, so nothing is
    withheld — but the identity tools have nothing to be about."""
    _fresh_db()
    srv = Server(MEMGRES_KEY_MODE="single")
    try:
        names = srv.tools(None)
        assert "memory_write" in names
        assert "memory_whoami" not in names
        assert "memory_list_spaces" not in names
        assert _admin(names) == []
    finally:
        srv.stop()


def test_a_pinned_token_still_decides_when_no_header_is_sent():
    """The env pin is how a stdio deployment names its caller, and it must keep
    working on http — it was the ONLY path the old tests covered."""
    _fresh_db()
    root = identity.new_token()
    boot = Server(MEMGRES_KEY_MODE="managed", MEMGRES_ADMIN_TOKEN=root,
                  MEMGRES_ADMIN_ROLE="superadmin")
    boot.stop()
    conn = psycopg.connect(DSN, autocommit=True)
    uid = identity.create_user(conn, name="worker")
    reader, _ = identity.issue_token(conn, uid, permission="read")
    conn.close()

    srv = Server(MEMGRES_KEY_MODE="managed", MEMGRES_ADMIN_TOKEN=root,
                 MEMGRES_ADMIN_ROLE="superadmin", MEMGRES_TOKEN=reader)
    try:
        assert "memory_write" not in srv.tools(None)
        # ...and a header still wins over the pin, as `_token` documents.
        assert "memory_write" in srv.tools(root)
    finally:
        srv.stop()


def test_visibility_off_shows_everything_to_everyone():
    _fresh_db()
    root = identity.new_token()
    boot = Server(MEMGRES_KEY_MODE="managed", MEMGRES_ADMIN_TOKEN=root,
                  MEMGRES_ADMIN_ROLE="superadmin")
    boot.stop()
    srv = Server(MEMGRES_KEY_MODE="managed", MEMGRES_ADMIN_TOKEN=root,
                 MEMGRES_ADMIN_ROLE="superadmin",
                 MEMGRES_MCP_TOOL_VISIBILITY="off")
    try:
        anonymous = srv.tools(None)
        assert "memory_write" in anonymous
        assert "memory_admin_set_role" in anonymous
        assert anonymous == srv.tools(root)
    finally:
        srv.stop()
