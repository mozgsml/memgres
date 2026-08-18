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
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
