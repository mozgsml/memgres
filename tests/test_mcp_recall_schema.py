"""memory_recall's advertised schema adapts to the deployment.

With no embedder (MEMGRES_EMBED_PROVIDER=none) there is no vector backend, so
semantic/hybrid recall can't run — the MCP tool schema must not offer them. We
inspect the FastMCP tool's `parameters` JSON schema the same way the server's
own best-effort pruning block reaches it.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("mcp")
pytest.importorskip("psycopg_pool")

from memgres.config import load  # noqa: E402
from memgres.mcp_server import build_server  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


def _clear_env(monkeypatch):
    # Each test builds a server against a differently-stamped collection, so
    # start from an empty schema — otherwise the stamp guard (rightly) refuses
    # the next build and the file only passes when some other test happened to
    # clean up first.
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_WORKER", "false")  # schema-only; no embedding


def _recall_mode_enum(mcp):
    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", {})
    tool = tools["memory_recall"]
    return tool.parameters["properties"]["mode"]["enum"]


def test_no_embedder_hides_vector_modes(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    mcp = build_server(load())
    enum = _recall_mode_enum(mcp)
    assert "semantic" not in enum and "hybrid" not in enum
    assert "lexical" in enum and "auto" in enum


def test_with_embedder_keeps_vector_modes(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "openai")   # cloud shape, not loaded
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", "stub")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", "3")
    monkeypatch.setenv("MEMGRES_EMBED_API_KEY", "x")
    mcp = build_server(load())
    enum = _recall_mode_enum(mcp)
    assert set(enum) == {"lexical", "semantic", "hybrid", "auto"}


def _space_schema(mcp, tool_name):
    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", {})
    return tools[tool_name].parameters["properties"]["space"]


def test_space_accepts_one_name_or_several(monkeypatch):
    """The advertised schema must let a model pass a LIST, not just a string —
    otherwise `space=["work","home"]` is rejected by tool-argument validation
    before the resolver ever sees it, and the multi-namespace address is
    unreachable over MCP no matter what the core supports."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    mcp = build_server(load())
    for tool in ("memory_recall", "memory_list"):
        for field in ("space", "space_id"):
            variants = _space_schema(mcp, tool).get("anyOf", [])
            kinds = {v.get("type") for v in variants}
            assert {"string", "array", "null"} <= kinds, f"{tool}.{field}: {variants}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ─── the merged search: one tool, and the half-search is really gone ─────────
def test_find_is_gone_and_recall_carries_its_job(monkeypatch):
    """`memory_find` searched titles and nothing else, so a caller had to choose
    between two half-searches — and on an uncaptioned corpus the title half
    answered "nothing found" to everything. Removing a tool is only done if the
    replacement is actually there, so both halves are asserted together."""
    _clear_env(monkeypatch)
    mcp = build_server()
    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", {})
    assert "memory_find" not in tools
    assert "bodies" in tools["memory_recall"].parameters["properties"]


def test_every_registered_tool_has_a_visibility_row(monkeypatch):
    """A registered tool missing from TOOL_VISIBILITY is one the per-caller
    filter cannot gate. The reverse is fine — the table also covers the identity
    and admin tools, which this single-mode server does not register."""
    from memgres.mcp_server import TOOL_VISIBILITY
    _clear_env(monkeypatch)
    mcp = build_server()
    tools = set(getattr(getattr(mcp, "_tool_manager", None), "_tools", {}))
    assert tools <= set(TOOL_VISIBILITY), tools - set(TOOL_VISIBILITY)
    assert "memory_find" not in TOOL_VISIBILITY
