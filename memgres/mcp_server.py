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

from . import identity
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

    def _uid(token: Optional[str]) -> str:
        """Resolve a token to its user id (for read-level identity tools)."""
        p = identity.resolve(conn, cfg, token)
        if p.user_id is None:
            raise identity.AuthError("this token has no owning user")
        return p.user_id

    def _admin_uid(token: Optional[str]) -> str:
        """Like _uid but for token management (issue/revoke/list). These are
        account-level admin actions, so they require an UNSCOPED admin-ceiling
        token — a read-only or namespace-scoped token must not mint/kill tokens
        or escalate its own scope/permission."""
        p = identity.resolve(conn, cfg, token)
        if p.user_id is None:
            raise identity.AuthError("this token has no owning user")
        if p.permission != "admin" or p.scope_namespace_id is not None:
            raise identity.AuthError(
                "token management requires an unscoped admin-ceiling token")
        return p.user_id

    @mcp.tool()
    def memory_write(body: Optional[str] = None, id: Optional[str] = None,
                     diff: Optional[str] = None, base_hash: Optional[str] = None,
                     path: Optional[str] = None, tags: Optional[List[str]] = None,
                     source: Optional[str] = None, reason: Optional[str] = None,
                     ttl_days: Optional[int] = None,
                     space: Optional[str] = None, space_id: Optional[str] = None,
                     token: Optional[str] = None) -> dict:
        """Create or edit a memory. Omit `id` to create (needs `body`). To edit,
        pass `id` plus either a whole new `body` or a unified `diff` with the
        `base_hash` it was cut from (stale base -> conflict). `path`/`tags` set
        the tree position and labels; `source`/`reason` record provenance.
        `space` picks one of your namespaces by name (`space_id` for a shared
        one); omit both to use your default."""
        return _mem(store.write(token, id=id or None, body=body, diff=diff,
                                base_hash=base_hash, path=path, tags=tags,
                                source=source, reason=reason, ttl_days=ttl_days,
                                space=space, space_id=space_id))

    @mcp.tool()
    def memory_get(id: str, space: Optional[str] = None,
                   space_id: Optional[str] = None,
                   token: Optional[str] = None) -> dict:
        """Fetch one memory by id (renews its TTL)."""
        return _mem(store.get(token, id, space=space, space_id=space_id))

    @mcp.tool()
    def memory_recall(query: str, k: int = 10, mode: str = "auto",
                      tags: Optional[List[str]] = None,
                      path_prefix: Optional[str] = None,
                      space: Optional[str] = None, space_id: Optional[str] = None,
                      token: Optional[str] = None) -> List[dict]:
        """Search memories. `mode`: lexical | semantic | hybrid | auto. Optionally
        scope to a tag set (`tags`) or a subtree (`path_prefix`, e.g. 'ops.postgres').
        `space`/`space_id` pick which namespace to search (default: yours)."""
        return [{"id": h.id, "body": h.body, "tags": h.tags, "path": h.path,
                 "score": h.score}
                for h in store.recall(token, query, k=k, tags=tags,
                                      path_prefix=path_prefix, mode=mode,
                                      space=space, space_id=space_id)]

    @mcp.tool()
    def memory_blame(id: str, grouped: bool = True,
                     space: Optional[str] = None, space_id: Optional[str] = None,
                     token: Optional[str] = None) -> List[dict]:
        """Who last changed each line. Grouped into author-blocks by default;
        set grouped=false for per-line attribution."""
        if grouped:
            return store.annotate_grouped(token, id, space=space, space_id=space_id)
        return store.annotate(token, id, space=space, space_id=space_id)

    @mcp.tool()
    def memory_history(id: str, space: Optional[str] = None,
                       space_id: Optional[str] = None,
                       token: Optional[str] = None) -> List[dict]:
        """The full change chain (diffs, provenance, hashes) for a memory."""
        return store.history(token, id, space=space, space_id=space_id)

    @mcp.tool()
    def memory_move(id: str, new_path: str, reason: Optional[str] = None,
                    space: Optional[str] = None, space_id: Optional[str] = None,
                    token: Optional[str] = None) -> dict:
        """Move a memory to a new tree path (cascades its subtree)."""
        return _mem(store.move(token, id, new_path, reason=reason,
                               space=space, space_id=space_id))

    @mcp.tool()
    def memory_forget(id: str, space: Optional[str] = None,
                      space_id: Optional[str] = None,
                      token: Optional[str] = None) -> dict:
        """Permanently delete a memory and its history (GDPR erasure)."""
        return {"forgotten": store.forget(token, id, space=space, space_id=space_id)}

    # ─── identity: spaces & tokens (open/managed modes) ─────────────────────
    @mcp.tool()
    def memory_list_spaces(token: Optional[str] = None) -> List[dict]:
        """List the namespaces this token can reach — your own plus any shared
        with you — with each one's id, name, description, permission and whether
        it's your default. Use a returned `id` as `space_id` to target a shared
        space; use your own space's `name` as `space`."""
        with conn.transaction():
            return identity.list_spaces(conn, _uid(token))

    @mcp.tool()
    def memory_issue_token(permission: str = "write", space: Optional[str] = None,
                           space_id: Optional[str] = None, label: str = "",
                           expires_days: Optional[int] = None,
                           token: Optional[str] = None) -> dict:
        """Mint a new token for your account (rotate / delegate / time-box).
        `permission` is its ceiling (read|write|admin); `space`/`space_id` scope
        it to one namespace (omit for all yours). The secret is returned ONCE."""
        import datetime as dt
        exp = None
        if expires_days:
            exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expires_days)
        with conn.transaction():
            uid = _admin_uid(token)
            nsid = space_id
            if nsid is not None:
                # can only scope a new token to a namespace the caller can reach
                with conn.cursor() as cur:
                    if identity._reach(cur, uid, nsid) is None:
                        raise identity.AuthError(
                            "cannot scope a token to an unreachable namespace")
            elif space is not None:
                nsid = identity.create_namespace(conn, uid, space)
            secret, tid = identity.issue_token(
                conn, uid, namespace_id=nsid, permission=permission,
                label=label, expires_at=exp)
        return {"token": secret, "id": tid, "permission": permission,
                "namespace_id": nsid, "note": "store this now — it is not recoverable"}

    @mcp.tool()
    def memory_list_tokens(token: Optional[str] = None) -> List[dict]:
        """List your tokens (metadata only — never the secret)."""
        with conn.transaction():
            out = identity.list_tokens(conn, _admin_uid(token))
        for d in out:                                  # make timestamps JSON-safe
            for key in ("expires_at", "revoked_at", "last_used_at", "created_at"):
                d[key] = str(d[key]) if d[key] else None
        return out

    @mcp.tool()
    def memory_revoke_token(token_id: str, token: Optional[str] = None) -> dict:
        """Revoke one of your tokens by id (kills it immediately)."""
        with conn.transaction():
            uid = _admin_uid(token)
            owned = {t["id"] for t in identity.list_tokens(conn, uid)}
            if token_id not in owned:
                raise identity.AuthError("not your token")
            return {"revoked": identity.revoke_token(conn, token_id)}

    return mcp


def main():  # pragma: no cover - entrypoint
    """stdio by default (a client spawns this process). Set
    MEMGRES_MCP_TRANSPORT=http to serve Streamable HTTP at MEMGRES_MCP_HOST:PORT
    (/mcp) instead — so the server can live in docker compose and clients just
    point at a URL."""
    import os

    server = build_server()
    transport = os.environ.get("MEMGRES_MCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http", "streamable_http"):
        server.settings.host = os.environ.get("MEMGRES_MCP_HOST", "0.0.0.0")
        server.settings.port = int(os.environ.get("MEMGRES_MCP_PORT", "8765"))
        server.run(transport="streamable-http")
    else:
        server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
