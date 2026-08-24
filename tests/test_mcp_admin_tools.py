"""The MCP control plane: it provisions, and it refuses everyone else.

These are the first tests in the repo that invoke MCP tools rather than
inspecting their schemas. That matters here: the security property is not "the
closure raises" but "the denial reaches the client as an error instead of a
successful result", and only a real `call_tool` shows the difference. Going
through the tool manager also exercises argument validation and JSON
conversion, which is where a datetime or a bad default would otherwise slip
through unnoticed.

The denial matrix is guarded for completeness — a new `memory_admin_*` tool
that nobody added to it fails the suite rather than shipping ungated.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("mcp")
pytest.importorskip("psycopg_pool")

from memgres import identity  # noqa: E402
from memgres.config import load  # noqa: E402
from memgres.mcp_server import build_server  # noqa: E402

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


# ─── harness ─────────────────────────────────────────────────────────────────

def _call(mcp, tool, /, **kw):
    """Invoke a tool the way a client does; return its payload.

    Positional-only, so a tool argument called `name` or `tool` cannot collide
    with this signature.
    """
    res = asyncio.run(mcp.call_tool(tool, kw))
    if isinstance(res, tuple):                      # mcp 1.x: (unstructured, structured)
        out = res[1]
    elif getattr(res, "structured_content", None) is not None:
        out = res.structured_content
    else:
        out = json.loads(res.content[0].text)
    if isinstance(out, dict) and set(out) == {"result"}:   # list returns get wrapped
        return out["result"]
    return out


@pytest.fixture
def box(monkeypatch):
    """A fresh managed deployment with a seeded superadmin, plus its server."""
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    root_tok = identity.new_token()
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_ADMIN_ROLE", "superadmin")
    monkeypatch.setenv("MEMGRES_ADMIN_TOKEN", root_tok)
    mcp = build_server(load())                      # migrates + seeds the admin
    return mcp, root_tok


def _admin_tools(mcp):
    return {n for n in mcp._tool_manager._tools if n.startswith("memory_admin_")}


# ─── the happy path: provisioning has to actually produce a usable account ───

def test_provisioning_end_to_end(box):
    """Every step through MCP, ending with the minted token doing real work.

    Asserting that rows appeared would miss the point: the failure this guards
    against is a user provisioned into a namespace whose token then writes
    somewhere else entirely.
    """
    mcp, root = box

    uid = _call(mcp, "memory_admin_create_user", name="ada", token=root)["id"]
    nsid = _call(mcp, "memory_admin_create_namespace", name="sales",
                 owner_user_id=uid, instruction="deals only", token=root)["id"]
    _call(mcp, "memory_admin_set_default_space", user_id=uid, space_id=nsid,
          token=root)
    minted = _call(mcp, "memory_admin_issue_token", user_id=uid,
                   permission="write", label="agent", token=root)

    # the directory reflects all of it
    users = _call(mcp, "memory_admin_list_users", token=root)
    assert [u for u in users if u["id"] == uid][0]["default_namespace_id"] == nsid
    assert [n["name"] for n in
            _call(mcp, "memory_admin_list_namespaces", token=root)] == ["sales"]

    # and the new credential is real: it writes, reads back, and knows itself
    who = _call(mcp, "memory_whoami", token=minted["token"])
    assert who["user_id"] == uid and who["permission"] == "write"
    assert who["capabilities"] == {"is_admin": False, "can_manage_users": False,
                                   "can_create_namespace": False}

    _call(mcp, "memory_write", body="a deal closed", path="deals.acme",
          token=minted["token"])
    got = _call(mcp, "memory_list", path_prefix="deals", token=minted["token"])
    assert [m["path"] for m in got] == ["deals.acme"]
    # it landed in the provisioned namespace, not in a lazily-created one
    assert [s["name"] for s in _call(mcp, "memory_list_spaces",
                                     token=minted["token"])] == ["sales"]

    # revoking it takes effect immediately
    _call(mcp, "memory_admin_revoke_token", token_id=minted["id"], token=root)
    with pytest.raises(ToolError):
        _call(mcp, "memory_whoami", token=minted["token"])


# ─── fail-closed: nobody without the role gets in, by any door ───────────────

def _matrix(uid: str, nsid: str, token_id: str) -> dict:
    """Every admin tool with well-formed arguments, so a refusal is about
    authorization and never about a malformed id."""
    return {
        "memory_admin_list_users": {},
        "memory_admin_create_user": {"name": "x"},
        "memory_admin_set_can_create_namespace": {"user_id": uid, "allowed": True},
        "memory_admin_set_role": {"user_id": uid, "role": "user_manager"},
        "memory_admin_list_namespaces": {},
        "memory_admin_create_namespace": {"name": "nope", "owner_user_id": uid},
        "memory_admin_edit_namespace": {"space_id": nsid, "description": "x"},
        "memory_admin_set_default_space": {"user_id": uid, "space_id": nsid},
        "memory_admin_add_member": {"space_id": nsid, "user_id": uid},
        "memory_admin_list_members": {"space_id": nsid},
        "memory_admin_issue_token": {"user_id": uid},
        "memory_admin_list_tokens": {"user_id": uid},
        "memory_admin_revoke_token": {"token_id": token_id},
    }


def test_denial_matrix_covers_every_admin_tool(box):
    """A new admin tool must be added to the matrix or this suite fails."""
    mcp, root = box
    uid = _call(mcp, "memory_admin_create_user", name="u", token=root)["id"]
    assert _admin_tools(mcp) == set(_matrix(uid, uid, uid))


def test_admin_tools_are_fail_closed(box):
    mcp, root = box

    victim = _call(mcp, "memory_admin_create_user", name="victim", token=root)["id"]
    nsid = _call(mcp, "memory_admin_create_namespace", name="private",
                 owner_user_id=victim, token=root)["id"]
    a_token = _call(mcp, "memory_admin_issue_token", user_id=victim,
                    token=root)["id"]

    plain = _call(mcp, "memory_admin_create_user", name="plain", token=root)["id"]
    # a plain user, at each of the ways a credential can fall short
    unprivileged = _call(mcp, "memory_admin_issue_token", user_id=plain,
                         permission="admin", token=root)["token"]
    read_only = _call(mcp, "memory_admin_issue_token", user_id=plain,
                      permission="read", token=root)["token"]
    scoped = _call(mcp, "memory_admin_issue_token", user_id=plain,
                   permission="admin", space_id=nsid, token=root)["token"]
    revoked_pair = _call(mcp, "memory_admin_issue_token", user_id=plain,
                         permission="admin", token=root)
    _call(mcp, "memory_admin_revoke_token", token_id=revoked_pair["id"], token=root)

    callers = {
        "plain user": unprivileged,
        "read-only": read_only,
        "namespace-scoped": scoped,
        "revoked": revoked_pair["token"],
        "unknown token": "mgk_" + "a" * 43,
        "no token": None,
    }
    for tool, args in _matrix(victim, nsid, a_token).items():
        for who, tok in callers.items():
            kw = dict(args)
            if tok is not None:
                kw["token"] = tok
            with pytest.raises(ToolError, match=r".*"):
                _call(mcp, tool, **kw)
            # and nothing happened: the victim keeps exactly one token
            assert len(_call(mcp, "memory_admin_list_tokens", user_id=victim,
                             token=root)) == 1, f"{tool} / {who} had an effect"


# ─── the middle tier: provisions without gaining ─────────────────────────────

def test_user_manager_provisions_but_does_not_escalate(box):
    mcp, root = box
    mgr = _call(mcp, "memory_admin_create_user", name="mgr",
                role="user_manager", token=root)["id"]
    mtok = _call(mcp, "memory_admin_issue_token", user_id=mgr,
                 permission="admin", token=root)["token"]

    # it does its job
    u = _call(mcp, "memory_admin_create_user", name="u", token=mtok)["id"]
    ns = _call(mcp, "memory_admin_create_namespace", name="kb",
               owner_user_id=u, token=mtok)["id"]
    _call(mcp, "memory_admin_issue_token", user_id=u, token=mtok)
    assert _call(mcp, "memory_admin_list_users", token=mtok)

    # but cannot mint authority, hand out cross-tenant access, or set roles
    for tool, args in (
        ("memory_admin_create_user", {"name": "x", "role": "superadmin"}),
        ("memory_admin_add_member", {"space_id": ns, "user_id": mgr}),
        ("memory_admin_set_role", {"user_id": mgr, "role": "superadmin"}),
    ):
        with pytest.raises(ToolError):
            _call(mcp, tool, token=mtok, **args)

    # nor touch the account that could undo any of this
    root_uid = _call(mcp, "memory_whoami", token=root)["user_id"]
    for tool, args in (
        ("memory_admin_issue_token", {"user_id": root_uid}),
        ("memory_admin_list_tokens", {"user_id": root_uid}),
    ):
        with pytest.raises(ToolError):
            _call(mcp, tool, token=mtok, **args)


# ─── role transitions ────────────────────────────────────────────────────────

def test_set_role_reaches_the_middle_tier_and_refuses_lockout(box):
    """`set_role` exists because grant/revoke only speak superadmin — there was
    no way to make a user_manager through any door."""
    mcp, root = box
    u = _call(mcp, "memory_admin_create_user", name="u", token=root)["id"]

    assert _call(mcp, "memory_admin_set_role", user_id=u, role="user_manager",
                 token=root)["role"] == "user_manager"
    assert [x["role"] for x in _call(mcp, "memory_admin_list_users",
                                     role="user_manager", token=root)] == \
        ["user_manager"]

    # demoting the only superadmin would leave nobody in charge
    root_uid = _call(mcp, "memory_whoami", token=root)["user_id"]
    with pytest.raises(ToolError):
        _call(mcp, "memory_admin_set_role", user_id=root_uid, role="user",
              token=root)

    # with a second superadmin it is allowed
    _call(mcp, "memory_admin_set_role", user_id=u, role="superadmin", token=root)
    assert _call(mcp, "memory_admin_set_role", user_id=root_uid, role="user",
                 token=root)["role"] == "user"


def test_edit_namespace_is_the_only_way_to_fix_a_routing_hint(box):
    """create_namespace is an upsert that ignores conflicts, so re-creating with
    corrected text silently does nothing."""
    mcp, root = box
    uid = _call(mcp, "memory_admin_create_user", name="u", token=root)["id"]
    ns = _call(mcp, "memory_admin_create_namespace", name="kb",
               owner_user_id=uid, instruction="typo", token=root)["id"]

    _call(mcp, "memory_admin_create_namespace", name="kb", owner_user_id=uid,
          instruction="fixed", token=root)
    assert _call(mcp, "memory_admin_list_namespaces",
                 token=root)[0]["instruction"] == "typo"

    _call(mcp, "memory_admin_edit_namespace", space_id=ns, instruction="fixed",
          token=root)
    assert _call(mcp, "memory_admin_list_namespaces",
                 token=root)[0]["instruction"] == "fixed"


def test_admin_surface_is_absent_where_there_is_nothing_to_administer(monkeypatch):
    """single mode has one implicit caller — no users, no namespaces, no tools."""
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "single")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    assert _admin_tools(build_server(load())) == set()

    monkeypatch.setenv("MEMGRES_MCP_ADMIN_TOOLS", "on")   # explicit beats auto
    assert _admin_tools(build_server(load()))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
