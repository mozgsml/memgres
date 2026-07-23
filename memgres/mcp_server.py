"""MCP server exposing memgres as agent tools (stdio).

Thin wrapper over the same `Store` the HTTP layer uses — an MCP client (Claude
Desktop, etc.) gets write / recall / get / blame / history / move / forget as
tools. Run it:

    memgres-mcp            # after: pip install "memgres[mcp]"

Requires MEMGRES_DATABASE_URL (or libpq PG* env) pointing at a Postgres the
schema can migrate into; migration runs once on startup.
"""

from __future__ import annotations

from typing import List, Optional

import psycopg

from .config import Config, load
from .embeddings import get_embedder
from .schema import migrate
from .store import Store


def _mcp(name: str):
    # The SDK renamed FastMCP -> MCPServer; support both.
    try:
        from mcp.server.mcpserver import MCPServer
        return MCPServer(name)
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        return FastMCP(name)


def _mem(m) -> dict:
    return {"id": m.id, "content_hash": m.content_hash, "body": m.body,
            "tags": m.tags, "path": m.path, "seq": m.seq,
            "created_at": str(m.created_at), "updated_at": str(m.updated_at),
            "expires_at": str(m.expires_at) if m.expires_at else None}


def build_server(cfg: Optional[Config] = None):
    cfg = cfg or load()
    conn = psycopg.connect(cfg.database_url or "")
    migrate(conn, cfg)
    store = Store(cfg, embedder=get_embedder(cfg), conn=conn)
    mcp = _mcp("memgres")

    @mcp.tool()
    def memory_write(body: Optional[str] = None, id: Optional[str] = None,
                     diff: Optional[str] = None, base_hash: Optional[str] = None,
                     path: Optional[str] = None, tags: Optional[List[str]] = None,
                     source: Optional[str] = None, reason: Optional[str] = None,
                     ttl_days: Optional[int] = None,
                     token: Optional[str] = None) -> dict:
        """Create or edit a memory. Omit `id` to create (needs `body`). To edit,
        pass `id` plus either a whole new `body` or a unified `diff` with the
        `base_hash` it was cut from (stale base -> conflict). `path`/`tags` set
        the tree position and labels; `source`/`reason` record provenance."""
        return _mem(store.write(token, id=id or None, body=body, diff=diff,
                                base_hash=base_hash, path=path, tags=tags,
                                source=source, reason=reason, ttl_days=ttl_days))

    @mcp.tool()
    def memory_get(id: str, token: Optional[str] = None) -> dict:
        """Fetch one memory by id (renews its TTL)."""
        return _mem(store.get(token, id))

    @mcp.tool()
    def memory_recall(query: str, k: int = 10, mode: str = "auto",
                      tags: Optional[List[str]] = None,
                      path_prefix: Optional[str] = None,
                      token: Optional[str] = None) -> List[dict]:
        """Search memories. `mode`: lexical | semantic | hybrid | auto. Optionally
        scope to a tag set (`tags`) or a subtree (`path_prefix`, e.g. 'ops.postgres')."""
        return [{"id": h.id, "body": h.body, "tags": h.tags, "path": h.path,
                 "score": h.score}
                for h in store.recall(token, query, k=k, tags=tags,
                                      path_prefix=path_prefix, mode=mode)]

    @mcp.tool()
    def memory_blame(id: str, grouped: bool = True,
                     token: Optional[str] = None) -> List[dict]:
        """Who last changed each line. Grouped into author-blocks by default;
        set grouped=false for per-line attribution."""
        if grouped:
            return store.annotate_grouped(token, id)
        return store.annotate(token, id)

    @mcp.tool()
    def memory_history(id: str, token: Optional[str] = None) -> List[dict]:
        """The full change chain (diffs, provenance, hashes) for a memory."""
        return store.history(token, id)

    @mcp.tool()
    def memory_move(id: str, new_path: str, reason: Optional[str] = None,
                    token: Optional[str] = None) -> dict:
        """Move a memory to a new tree path (cascades its subtree)."""
        return _mem(store.move(token, id, new_path, reason=reason))

    @mcp.tool()
    def memory_forget(id: str, token: Optional[str] = None) -> dict:
        """Permanently delete a memory and its history (GDPR erasure)."""
        return {"forgotten": store.forget(token, id)}

    return mcp


def main():  # pragma: no cover - entrypoint
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
