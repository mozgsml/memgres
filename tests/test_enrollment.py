"""Enrollment: claim an account with a one-time key, keeping the secret home.

The property under test is not "a token gets created" — it is that the token is
created on the CLIENT side and the server only ever learns its hash. So the
tests go through the MCP door the way a real client does, with the credential
configured rather than passed, and the sharpest assertion in the file is the one
about the tool's SCHEMA: `memory_enroll` must not accept the token as an
argument, because an argument is transcript.

The rest is the lifecycle a stolen key depends on being wrong: single use, an
expiry the database decides, a revoked key, and a credential the server already
knows being refused rather than silently re-bound.
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

from memgres import admin, identity  # noqa: E402
from memgres.config import load  # noqa: E402
from memgres.mcp_server import build_server, visible_tools  # noqa: E402

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


def _call(mcp, tool, /, **kw):
    res = asyncio.run(mcp.call_tool(tool, kw))
    if isinstance(res, tuple):
        out = res[1]
    elif isinstance(res, dict):
        out = res
    elif getattr(res, "structured_content", None) is not None:
        out = res.structured_content
    else:
        out = json.loads(getattr(res, "content", res)[0].text)
    if isinstance(out, dict) and set(out) == {"result"}:
        return out["result"]
    return out


@pytest.fixture
def box(monkeypatch):
    """A managed deployment, plus a way to be any client you like."""
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    root = identity.new_token()
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    monkeypatch.setenv("MEMGRES_ADMIN_ROLE", "superadmin")
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    monkeypatch.setenv("MEMGRES_ADMIN_TOKEN", root)
    built = {}

    def as_(tok):
        """A server configured with `tok` — which is how a real client carries a
        credential, and the only way `memory_enroll` can see one."""
        if tok not in built:
            if tok is None:
                monkeypatch.delenv("MEMGRES_TOKEN", raising=False)
            else:
                monkeypatch.setenv("MEMGRES_TOKEN", tok)
            built[tok] = build_server(load())
        return built[tok]

    as_(root)                                   # migrates + seeds the admin
    return as_, root


def _provision(as_, root, name="ada", **kw):
    """An account with a namespace, ready to be claimed."""
    uid = _call(as_(root), "memory_admin_create_user", name=name)["id"]
    nsid = _call(as_(root), "memory_admin_create_namespace",
                 name=f"{name}space", owner_user_id=uid)["id"]
    key = _call(as_(root), "memory_admin_create_enrollment",
                user_id=uid, space_id=nsid, **kw)
    return uid, nsid, key


# ─── the invariant the whole design rests on ─────────────────────────────────

def test_enroll_does_not_accept_the_token_as_an_argument(box):
    """If the secret could be passed in, an agent would pass it in — and the
    conversation would hold the credential we went to all this trouble to keep
    out of it. The tool takes the key and nothing else."""
    as_, root = box
    tool = as_(root)._tool_manager._tools["memory_enroll"]
    schema = getattr(tool, "parameters", None) or tool.fn_metadata.arg_model.model_json_schema()
    assert set(schema.get("properties", {})) == {"key"}


def test_the_key_is_never_echoed_back_by_the_listing(box):
    as_, root = box
    uid, _, key = _provision(as_, root)
    rows = _call(as_(root), "memory_admin_list_enrollments", user_id=uid)
    assert rows and key["key"] not in json.dumps(rows)
    assert rows[0]["state"] == "pending"


# ─── the happy path, end to end ──────────────────────────────────────────────

def test_a_self_generated_token_becomes_a_working_credential(box):
    as_, root = box
    uid, nsid, key = _provision(as_, root, permission="write")

    mine = identity.new_token()                 # generated HERE, like a client
    out = _call(as_(mine), "memory_enroll", key=key["key"])
    assert out["user_id"] == uid and out["namespace_id"] == nsid

    # and it really works, in the namespace the KEY chose
    _call(as_(mine), "memory_write", body="hello", path="a", title="a")
    assert _call(as_(mine), "memory_get", at="a")["body"] == "hello"
    who = _call(as_(mine), "memory_whoami")
    assert who["user_id"] == uid


def test_the_ceiling_comes_from_the_key_not_from_the_redeemer(box):
    """Enrolling must not be a way to ask for more than you were given."""
    as_, root = box
    _, _, key = _provision(as_, root, permission="read", name="reader")
    mine = identity.new_token()
    assert _call(as_(mine), "memory_enroll", key=key["key"])["permission"] == "read"
    with pytest.raises(ToolError):
        _call(as_(mine), "memory_write", body="x", path="b", title="b")


def test_the_server_stores_a_hash_and_not_the_secret(box):
    as_, root = box
    _, _, key = _provision(as_, root)
    mine = identity.new_token()
    _call(as_(mine), "memory_enroll", key=key["key"])
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT token_hash FROM token")
        stored = [r[0] for r in cur.fetchall()]
    assert identity.token_hash(mine) in stored
    assert mine not in stored


# ─── what a stolen key runs into ─────────────────────────────────────────────

def test_a_key_works_once_and_says_so_afterwards(box):
    """The spent-key message IS the theft alarm — it is the only signal a stolen
    key leaves behind, and the first token stays valid, which is what makes the
    alarm actionable."""
    as_, root = box
    uid, _, key = _provision(as_, root)
    first, second = identity.new_token(), identity.new_token()
    _call(as_(first), "memory_enroll", key=key["key"])

    with pytest.raises(ToolError, match="already redeemed"):
        _call(as_(second), "memory_enroll", key=key["key"])

    _call(as_(first), "memory_write", body="still mine", path="c", title="c")
    rows = _call(as_(root), "memory_admin_list_enrollments", user_id=uid)
    assert rows[0]["state"] == "redeemed" and rows[0]["used_token_id"]


def test_an_expired_key_is_refused_by_the_database_clock(box):
    as_, root = box
    uid, _, key = _provision(as_, root, expires_minutes=1)
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("UPDATE enrollment_key SET expires_at = now() - interval '1 s'")
    with pytest.raises(ToolError, match="expired"):
        _call(as_(identity.new_token()), "memory_enroll", key=key["key"])
    assert _call(as_(root), "memory_admin_list_enrollments",
                 user_id=uid)[0]["state"] == "expired"


def test_a_revoked_key_is_refused(box):
    as_, root = box
    uid, _, key = _provision(as_, root)
    assert _call(as_(root), "memory_admin_revoke_enrollment",
                 enrollment_id=key["id"])["revoked"] is True
    with pytest.raises(ToolError, match="revoked"):
        _call(as_(identity.new_token()), "memory_enroll", key=key["key"])
    # and revoking a SPENT key is refused rather than pretended
    uid2, _, key2 = _provision(as_, root, name="bob")
    _call(as_(identity.new_token()), "memory_enroll", key=key2["key"])
    assert _call(as_(root), "memory_admin_revoke_enrollment",
                 enrollment_id=key2["id"])["revoked"] is False


def test_a_credential_the_server_already_knows_cannot_be_rebound(box):
    """Otherwise revocation would be a suggestion: a revoked token could be
    enrolled onto another account and live again."""
    as_, root = box
    uid, _, key = _provision(as_, root)
    mine = identity.new_token()
    _call(as_(mine), "memory_enroll", key=key["key"])

    _, _, key2 = _provision(as_, root, name="carl")
    with pytest.raises(ToolError, match="already known"):
        _call(as_(mine), "memory_enroll", key=key2["key"])


def test_a_malformed_credential_is_refused_before_anything_is_spent(box):
    as_, root = box
    uid, _, key = _provision(as_, root)
    with pytest.raises(ToolError):
        _call(as_("not-a-token"), "memory_enroll", key=key["key"])
    # the key survived the failed attempt
    assert _call(as_(root), "memory_admin_list_enrollments",
                 user_id=uid)[0]["state"] == "pending"


# ─── what an unbound client is shown ─────────────────────────────────────────

def test_is_unbound_is_true_only_for_a_credential_nobody_owns(box):
    """The decision behind the enrollment surface, tested where it is made.

    Whether a client SEES `memory_enroll` is decided by this predicate on every
    `tools/list`; the wire-level proof that the list changes lives in
    test_mcp_tool_visibility_http.py, because an in-process `list_tools()` does
    not go through the request handler the filter is installed on.

    Every False here is a security property, not a nicety: a revoked or expired
    token that answered True could bind itself to a fresh account and outlive
    its own revocation.
    """
    as_, root = box
    cfg = load()
    _, _, key = _provision(as_, root)
    mine = identity.new_token()
    with psycopg.connect(DSN) as conn:
        assert identity.is_unbound(conn, cfg, mine) is True
        assert identity.is_unbound(conn, cfg, "not-a-token") is False
        assert identity.is_unbound(conn, cfg, None) is False
        assert identity.is_unbound(conn, cfg, root) is False      # the admin's own

    _call(as_(mine), "memory_enroll", key=key["key"])
    with psycopg.connect(DSN) as conn:
        assert identity.is_unbound(conn, cfg, mine) is False      # now it is known

        # revoked and expired are KNOWN credentials; enrolling is not a way out
        uid2 = identity.create_user(conn, "dora")
        revoked_secret, tid = identity.issue_token(conn, uid2)
        identity.revoke_token(conn, tid)
        expired_secret, _ = identity.issue_token(conn, uid2)
        with conn.cursor() as cur:
            cur.execute("UPDATE token SET expires_at = now() - interval '1 day' "
                        "WHERE token_hash=%s", (identity.token_hash(expired_secret),))
        conn.commit()
        assert identity.is_unbound(conn, cfg, revoked_secret) is False
        assert identity.is_unbound(conn, cfg, expired_secret) is False


def test_is_unbound_is_false_wherever_enrolling_is_meaningless(box, monkeypatch):
    """`open` mode already accepts any well-formed token — a key would be
    ceremony around a door that is not locked. `single` has no identities."""
    as_, root = box
    mine = identity.new_token()
    with psycopg.connect(DSN) as conn:
        for mode in ("open", "single"):
            monkeypatch.setenv("MEMGRES_KEY_MODE", mode)
            assert identity.is_unbound(conn, load(), mine) is False


def test_visible_tools_unbound_is_an_allowlist():
    """Unit-level, because the allowlist inverts the module's usual rule and a
    new tool must not appear in front of an unbound caller by default."""
    names = ["memory_enroll", "memory_server_info", "memory_write",
             "memory_recall", "memory_admin_create_user", "something_new"]
    assert visible_tools(names, None, True, unbound=True) == [
        "memory_enroll", "memory_server_info"]


# ─── who may issue one ───────────────────────────────────────────────────────

def test_a_plain_user_cannot_issue_enrollment_keys(box):
    as_, root = box
    uid, nsid, key = _provision(as_, root)
    mine = identity.new_token()
    _call(as_(mine), "memory_enroll", key=key["key"])
    with pytest.raises(ToolError):
        _call(as_(mine), "memory_admin_create_enrollment", user_id=uid)


def test_a_user_manager_cannot_issue_one_for_an_admin_account(box):
    """Same rule as issuing a token: a provisioning tier that could mint a key
    for a superadmin would be a superadmin."""
    as_, root = box
    with psycopg.connect(DSN) as conn:
        boss = identity.create_user(conn, "boss", role="superadmin")
        mgr = identity.create_user(conn, "mgr", role="user_manager")
        secret, _ = identity.issue_token(conn, mgr, permission="admin")
        conn.commit()
    with pytest.raises(ToolError):
        _call(as_(secret), "memory_admin_create_enrollment", user_id=boss)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
