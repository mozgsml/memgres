"""What a client is SHOWN, as opposed to what it is allowed.

The tool list an MCP client receives is answered per caller: a read-only agent
is not offered five write tools it will only ever be refused, and a deployment
with no identities does not advertise namespace and token management.

Two properties are load-bearing, and the tests are split along them:

* the POLICY (which capability each tool needs) is a pure table, tested
  exhaustively and cheaply;
* the WIRING is tested once against a real server, including the property that
  matters most — hiding is not authorization. A client that ignores the list and
  calls a hidden tool is refused by the service layer, not by a missing entry in
  a lookup table. If that ever inverted, the refusal would arrive as "unknown
  tool" and a legitimate caller with changed rights would look broken.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("mcp")
pytest.importorskip("psycopg_pool")

from mcp import types as mcp_types  # noqa: E402

from memgres import identity  # noqa: E402
from memgres.config import load  # noqa: E402
from memgres.mcp_server import (  # noqa: E402
    TOOL_VISIBILITY, build_server, visible_tools,
)

try:                                    # mcp 2.x
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")

ALL_CAPS = {"can_write": True, "can_create_namespace": True,
            "can_manage_users": True, "can_manage_own_tokens": True,
            "can_administer_deployment": True, "has_admin_ceiling": True,
            "is_admin": True}
NO_CAPS = {k: False for k in ALL_CAPS}


# ─── the policy table ────────────────────────────────────────────────────────
def test_a_read_only_credential_is_shown_no_write_tools():
    caps = dict(NO_CAPS)
    shown = visible_tools(TOOL_VISIBILITY, caps, identity_on=True)
    assert "memory_recall" in shown and "memory_get" in shown
    for w in ("memory_write", "memory_move", "memory_forget"):
        assert w not in shown


def test_a_plain_writer_is_shown_no_control_plane():
    caps = dict(NO_CAPS, can_write=True)
    shown = visible_tools(TOOL_VISIBILITY, caps, identity_on=True)
    assert "memory_write" in shown
    assert not [t for t in shown if t.startswith("memory_admin_")]
    assert "memory_issue_token" not in shown          # needs an unscoped admin token
    assert "memory_create_space" not in shown         # needs the right
    assert "memory_whoami" in shown                   # always worth asking


def test_the_two_admin_tiers_are_distinguished():
    manager = visible_tools(TOOL_VISIBILITY,
                            dict(NO_CAPS, can_manage_users=True), True)
    assert "memory_admin_create_user" in manager
    # provisioning does not include cross-tenant reach or handing out roles
    for root_only in ("memory_admin_set_role", "memory_admin_add_member",
                      "memory_admin_adopt_orphans"):
        assert root_only not in manager

    root = visible_tools(TOOL_VISIBILITY, ALL_CAPS, True)
    assert set(root) == set(TOOL_VISIBILITY)          # a superadmin sees it all


def test_without_identities_only_the_identity_tools_go():
    """`single` mode has one implicit caller: everything is permitted, and
    namespaces, tokens and users do not exist to be managed."""
    shown = visible_tools(TOOL_VISIBILITY, ALL_CAPS, identity_on=False)
    assert "memory_write" in shown and "memory_recall" in shown
    assert "memory_list_spaces" not in shown
    assert not [t for t in shown if t.startswith("memory_admin_")]


def test_an_unidentified_caller_still_sees_a_working_server():
    """A bad or missing token hides everything conditional, but the read surface
    stays — so a misconfigured client gets a real authentication error on call
    instead of an empty, broken-looking server."""
    shown = visible_tools(TOOL_VISIBILITY, None, identity_on=True)
    assert "memory_recall" in shown
    assert "memory_write" not in shown
    assert not [t for t in shown if t.startswith("memory_admin_")]


def test_an_unlisted_tool_is_shown_rather_than_hidden():
    """Forgetting an entry must not make a tool vanish: a missing tool is the
    failure nobody notices, a refused call is one the caller can read."""
    assert visible_tools(["memory_brand_new"], NO_CAPS, True) == ["memory_brand_new"]


# ─── the wiring ──────────────────────────────────────────────────────────────
def _fresh_db():
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def _env(monkeypatch, **extra):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def _listed(mcp):
    """The tool names a client actually receives, through the REAL handler.

    Not `mcp.list_tools()` blindly: on mcp 1.x the low-level server captured the
    bound method at construction, so calling the attribute would bypass the
    filter and this would test nothing. Each generation is walked the way its
    own request path walks it — which is the difference that let the feature
    ship dead on 2.x while every local test passed on 1.x.
    """
    if hasattr(mcp, "_mcp_server"):                          # mcp 1.x
        handler = mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest]
        result = asyncio.run(handler(None))
        return [t.name for t in result.root.tools]
    return [t.name for t in asyncio.run(mcp.list_tools())]   # mcp 2.x


@pytest.fixture
def deployment(monkeypatch):
    """A managed deployment with a superadmin, a plain writer and a reader."""
    _fresh_db()
    root_tok = identity.new_token()
    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok,
         MEMGRES_ADMIN_ROLE="superadmin")
    build_server(load())                       # migrates + seeds the superadmin
    conn = psycopg.connect(DSN, autocommit=True)
    uid = identity.create_user(conn, name="worker")
    identity.create_namespace(conn, uid, "work")
    writer, _ = identity.issue_token(conn, uid, permission="write")
    reader, _ = identity.issue_token(conn, uid, permission="read")
    yield root_tok, writer, reader
    conn.close()


def test_the_pinned_token_decides_what_this_endpoint_shows(deployment,
                                                           monkeypatch):
    root_tok, writer, reader = deployment

    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=reader)
    assert "memory_write" not in _listed(build_server(load()))

    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=writer)
    as_writer = _listed(build_server(load()))
    assert "memory_write" in as_writer
    assert not [t for t in as_writer if t.startswith("memory_admin_")]

    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=root_tok)
    as_root = _listed(build_server(load()))
    assert "memory_admin_create_user" in as_root and "memory_write" in as_root


def test_hiding_a_tool_is_not_how_it_is_refused(deployment, monkeypatch):
    """The property that keeps this feature honest. A hidden tool called anyway
    must fail on AUTHORIZATION, with a message about permission — not vanish
    into "unknown tool", which would make a rights change look like a broken
    server and put the real check in a display table."""
    root_tok, writer, reader = deployment
    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=reader)
    mcp = build_server(load())

    assert "memory_write" not in _listed(mcp)
    with pytest.raises(ToolError) as e:
        asyncio.run(mcp.call_tool("memory_write", {"body": "should not land\n"}))
    assert "unknown tool" not in str(e.value).lower()

    # and it did not land — the refusal is the service layer's, not the list's
    conn = psycopg.connect(DSN, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memory WHERE body LIKE %s",
                        ("should not land%",))
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()

    # a tool the same credential IS shown works, so the refusal above was about
    # permission and not about the server being broken for this client
    assert asyncio.run(mcp.call_tool("memory_recall", {"query": "anything"})) \
        is not None


def test_a_narrowed_credential_narrows_the_list_even_for_a_superadmin(
        deployment, monkeypatch):
    """A superadmin holding a scoped token cannot provision with it, and the
    list says so. This is the same lie `whoami` used to tell by reporting the
    ROLE's potential — the pair of them sent a caller at doors that refuse it."""
    root_tok, _, _ = deployment
    conn = psycopg.connect(DSN, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM token WHERE token_hash=%s",
                        (identity.token_hash(root_tok),))
            root_uid = str(cur.fetchone()[0])
        nsid = identity.create_namespace(conn, root_uid, "root-own")
        scoped, _ = identity.issue_token(conn, root_uid, namespace_id=nsid,
                                         permission="admin")
    finally:
        conn.close()

    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=scoped)
    shown = _listed(build_server(load()))
    assert "memory_admin_create_user" not in shown      # provisioning is gone
    assert "memory_issue_token" not in shown            # so is minting tokens
    assert "memory_admin_edit_namespace" in shown       # its own namespace remains
    assert "memory_write" in shown


def test_a_database_blip_fails_the_listing_rather_than_shrinking_it(deployment,
                                                                   monkeypatch):
    """Swallowing every failure into "unidentified caller" answers with the
    read-only subset. A client lists once at connect and caches, so a one-second
    blip would take an agent's write tools away for the whole session and the
    agent would report the server as read-only. Failing is recoverable; a wrong
    list is not."""
    root_tok, writer, _ = deployment
    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=writer)
    mcp = build_server(load())
    assert "memory_write" in _listed(mcp)

    import memgres.mcp_server as ms

    real = ms.identity.resolve

    def explode(conn, cfg, secret, **kw):
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(ms.identity, "resolve", explode)
    try:
        with pytest.raises(RuntimeError):
            _listed(mcp)
    finally:
        monkeypatch.setattr(ms.identity, "resolve", real)

    # a token that simply fails to authenticate is NOT a blip: it answers
    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=identity.new_token())
    shown = _listed(build_server(load()))
    assert "memory_recall" in shown and "memory_write" not in shown


def test_listing_tools_does_not_count_as_using_the_token(deployment, monkeypatch):
    """`resolve` stamps `last_used_at`. Listing is asking ABOUT the credential,
    not acting on it — counting it would make every `tools/list` a write
    transaction and turn the column into "last connected"."""
    root_tok, writer, _ = deployment
    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=writer)
    mcp = build_server(load())

    conn = psycopg.connect(DSN, autocommit=True)
    try:
        def last_used():
            with conn.cursor() as cur:
                cur.execute("SELECT last_used_at FROM token WHERE token_hash=%s",
                            (identity.token_hash(writer),))
                return cur.fetchone()[0]

        _listed(mcp)
        assert last_used() is None
        asyncio.run(mcp.call_tool("memory_recall", {"query": "anything"}))
        assert last_used() is not None          # a real call still stamps it
    finally:
        conn.close()


def test_visibility_can_be_turned_off(deployment, monkeypatch):
    root_tok, writer, reader = deployment
    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=reader, MEMGRES_MCP_TOOL_VISIBILITY="off")
    assert "memory_write" in _listed(build_server(load()))


def test_this_sdk_still_exposes_a_hook_the_filter_can_attach_to(deployment,
                                                               monkeypatch):
    """The canary for the failure that actually happened: the filter was
    installed inside a bare `except Exception: pass`, so on an SDK that had
    moved its internals it attached to NOTHING — every client saw every tool,
    with no error and a green local suite (the local SDK was a generation
    behind). An SDK that offers neither hook must be loud, not silent."""
    root_tok, _, _ = deployment
    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=root_tok)
    mcp = build_server(load())
    assert hasattr(mcp, "_handle_list_tools") or hasattr(mcp, "_mcp_server"), (
        "neither known list-tools hook is present — the visibility filter has "
        "nothing to attach to on this SDK")


def test_every_registered_tool_has_a_visibility_entry(deployment, monkeypatch):
    """Completeness, kept by the suite rather than by memory: a new tool that
    nobody classified defaults to visible, which is safe but silent."""
    root_tok, _, _ = deployment
    _env(monkeypatch, MEMGRES_ADMIN_TOKEN=root_tok, MEMGRES_ADMIN_ROLE="superadmin",
         MEMGRES_TOKEN=root_tok)
    mcp = build_server(load())
    registered = set(mcp._tool_manager._tools)
    assert registered - set(TOOL_VISIBILITY) == set()
    assert set(TOOL_VISIBILITY) - registered == set()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
