"""A minted secret must be able to reach its operator WITHOUT passing through
the caller.

The caller is increasingly an agent, and an agent's reply is a transcript: it is
logged, summarized, replayed into a model's context and shipped to a provider.
`MEMGRES_TOKEN_SINK` diverts the secret to a file on the server, leaving the
reply carrying only a path.

The interesting failures are not in the writing — they are in the WIRING. A door
that quietly keeps returning the secret while the sink is configured looks
exactly like a working deployment, and the leak is invisible in every test that
only checks the file appeared. So each door here is asserted twice: the reply
does NOT carry the secret, and the file DOES hold a credential that really
works.
"""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memgres import admin, identity  # noqa: E402
from memgres.config import load  # noqa: E402


# ─── the sink itself: filesystem only, no database ───────────────────────────

def test_the_secret_lands_in_a_private_file_under_a_private_directory(tmp_path):
    sink = tmp_path / "tokens"
    path = identity.stash_secret(str(sink), "tok-1", "mgk_abc")
    assert Path(path).read_text().strip() == "mgk_abc"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(sink).st_mode) == 0o700


def test_an_existing_loose_file_is_tightened_rather_than_trusted(tmp_path):
    """The sink may be a directory an operator made by hand — possibly 0755,
    possibly with a stale world-readable file already in it. Writing into it
    without re-imposing the mode would publish the new secret."""
    sink = tmp_path / "tokens"
    sink.mkdir(mode=0o755)
    (sink / "tok-1.token").write_text("stale")
    os.chmod(sink / "tok-1.token", 0o644)
    path = identity.stash_secret(str(sink), "tok-1", "mgk_fresh")
    assert Path(path).read_text().strip() == "mgk_fresh"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_reply_shape_switches_on_the_sink(tmp_path):
    plain = admin.deliver_secret("mgk_s", "tok-1", "")
    assert plain["token"] == "mgk_s" and plain["id"] == "tok-1"

    sunk = admin.deliver_secret("mgk_s", "tok-1", str(tmp_path))
    assert "token" not in sunk                      # the whole point
    assert sunk["id"] == "tok-1" and sunk["delivered"] == "file"
    assert Path(sunk["path"]).read_text().strip() == "mgk_s"
    assert "mgk_s" not in json.dumps(sunk)


def test_a_relative_sink_is_refused_at_config_time(monkeypatch):
    """Two processes with different working directories would write the same
    operator's secrets to two different places, and neither would say which."""
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_TOKEN_SINK", "tokens")
    with pytest.raises(ValueError, match="absolute"):
        load()


# ─── the doors: MCP, where the caller is the agent ───────────────────────────

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("mcp")
pytest.importorskip("psycopg_pool")

from memgres.mcp_server import build_server  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


needs_pg = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


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
def box(monkeypatch, tmp_path):
    """A managed deployment whose sink is set BEFORE the server is built, since
    that is when the config a tool closes over is read."""
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
    sink = tmp_path / "sink"
    built = {}

    def make(tok, *, with_sink=True):
        key = (tok, with_sink)
        if key not in built:
            if with_sink:
                monkeypatch.setenv("MEMGRES_TOKEN_SINK", str(sink))
            else:
                monkeypatch.delenv("MEMGRES_TOKEN_SINK", raising=False)
            if tok is None:
                monkeypatch.delenv("MEMGRES_TOKEN", raising=False)
            else:
                monkeypatch.setenv("MEMGRES_TOKEN", tok)
            built[key] = build_server(load())
        return built[key]

    make(root)                                      # migrates + seeds the admin
    return make, root, sink


@needs_pg
def test_the_admin_door_hands_back_a_path_and_the_file_really_authenticates(box):
    make, root, sink = box
    uid = _call(make(root), "memory_admin_create_user", name="ada")["id"]
    nsid = _call(make(root), "memory_admin_create_namespace",
                 name="sales", owner_user_id=uid)["id"]
    minted = _call(make(root), "memory_admin_issue_token", user_id=uid,
                   permission="write", space_id=nsid, label="agent")

    assert "token" not in minted and minted["delivered"] == "file"
    secret = Path(minted["path"]).read_text().strip()
    assert secret not in json.dumps(minted)         # not smuggled in the note

    # The file holds the real credential, not a rendering of one.
    _call(make(secret), "memory_write", body="hello", path="a", title="a")
    got = _call(make(secret), "memory_get", at="a")
    assert got["body"] == "hello"


@needs_pg
def test_the_self_service_door_is_wired_too(box):
    """Two doors mint tokens. The admin one is the one everybody remembers."""
    make, root, sink = box
    uid = _call(make(root), "memory_admin_create_user", name="bob")["id"]
    _call(make(root), "memory_admin_create_namespace", name="bobspace",
          owner_user_id=uid)
    first = _call(make(root), "memory_admin_issue_token", user_id=uid,
                  permission="admin")
    bob = Path(first["path"]).read_text().strip()

    rotated = _call(make(bob), "memory_issue_token", permission="write",
                    label="laptop")
    assert "token" not in rotated and rotated["delivered"] == "file"
    assert rotated["permission"] == "write"         # the door's own fields survive
    secret = Path(rotated["path"]).read_text().strip()
    assert secret.startswith("mgk_") and secret != bob


@needs_pg
def test_without_a_sink_the_secret_is_still_returned(box):
    """A deployment that never set the sink must keep working — the reply is the
    only channel it has, and refusing to use it would break provisioning."""
    make, root, sink = box
    uid = _call(make(root), "memory_admin_create_user", name="carl")["id"]
    minted = _call(make(root, with_sink=False), "memory_admin_issue_token",
                   user_id=uid)
    assert minted["token"].startswith("mgk_")
    assert not sink.exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
