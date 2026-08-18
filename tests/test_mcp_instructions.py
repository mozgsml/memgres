"""Server-side MCP `instructions` come from MEMGRES_INSTRUCTION.

The initialize response can carry an operator-supplied instructions string; a
client that honors it (e.g. Claude Code) loads it once at connect. It's optional
(no env → the SDK omits the field) and byte-capped so it stays small.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memgres.mcp_server import (MCP_INSTRUCTION_MAX_BYTES,  # noqa: E402
                                _instruction_text)


def _clear(monkeypatch):
    monkeypatch.delenv("MEMGRES_INSTRUCTION", raising=False)


# ─── the pure text resolver (no DB) ──────────────────────────────────────────
def test_absent_env_is_none(monkeypatch):
    _clear(monkeypatch)
    assert _instruction_text() is None


def test_blank_env_is_none(monkeypatch):
    monkeypatch.setenv("MEMGRES_INSTRUCTION", "   \n\t ")
    assert _instruction_text() is None      # whitespace-only → omit, not ""


def test_plain_text_passes_through(monkeypatch):
    monkeypatch.setenv("MEMGRES_INSTRUCTION", "  Write ops decisions under ops.*  ")
    assert _instruction_text() == "Write ops decisions under ops.*"   # trimmed


def test_capped_on_utf8_boundary(monkeypatch):
    # A multibyte body longer than the cap: truncated to <= cap bytes, still valid
    # UTF-8 (no split code point).
    big = "я" * MCP_INSTRUCTION_MAX_BYTES        # 2 bytes each → 2x over the cap
    monkeypatch.setenv("MEMGRES_INSTRUCTION", big)
    out = _instruction_text()
    b = out.encode("utf-8")
    assert len(b) <= MCP_INSTRUCTION_MAX_BYTES
    assert out == out.encode("utf-8").decode("utf-8")   # valid, no partial char
    assert set(out) == {"я"}


def test_exactly_at_cap_kept(monkeypatch):
    exact = "a" * MCP_INSTRUCTION_MAX_BYTES         # 1 byte each, ascii
    monkeypatch.setenv("MEMGRES_INSTRUCTION", exact)
    assert _instruction_text() == exact


# ─── wired into the built server (needs Postgres) ────────────────────────────
psycopg = pytest.importorskip("psycopg")
pytest.importorskip("mcp")
pytest.importorskip("psycopg_pool")

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


def _clear_all(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_WORKER", "false")  # instructions-only


@pytest.mark.skipif(not _reachable(), reason="no test Postgres")
def test_build_server_sets_instructions(monkeypatch):
    from memgres.config import load
    from memgres.mcp_server import build_server
    _clear_all(monkeypatch)
    monkeypatch.setenv("MEMGRES_INSTRUCTION", "Prefer memory_find before recall.")
    mcp = build_server(load())
    assert mcp.instructions == "Prefer memory_find before recall."


@pytest.mark.skipif(not _reachable(), reason="no test Postgres")
def test_build_server_no_instructions_is_none(monkeypatch):
    from memgres.config import load
    from memgres.mcp_server import build_server
    _clear_all(monkeypatch)
    mcp = build_server(load())
    assert mcp.instructions is None      # field omitted when env unset
