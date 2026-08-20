"""MCP server exposing memgres as agent tools (stdio or Streamable HTTP).

Thin wrapper over the same `Store` the HTTP layer uses — an MCP client (Claude
Desktop, etc.) gets write / recall / get / blame / history / move / forget as
tools. Run it:

    memgres-mcp            # after: pip install "memgres[mcp]"

Requires MEMGRES_DATABASE_URL (or libpq PG* env) pointing at a Postgres the
schema can migrate into; migration runs once on startup.

**The caller's identity is pinned in the MCP client config, never handled by the
LLM.** The tools take no `token` argument. The token is resolved server-side:

  * http transport — the ``Authorization: Bearer <mgk_…>`` (or ``X-Memgres-Token``)
    header the client sends, so one shared endpoint serves many clients, each
    pinned to its own user via its own config ``headers``;
  * else the ``MEMGRES_TOKEN`` env default (a stdio client sets it in its config
    ``env`` block; a dedicated http endpoint sets it on the service).

So the agent works as one fixed user, spends no tokens echoing a secret, and
can't switch users. Pin a *namespace-scoped* token and it can't switch space
either. (``single`` mode needs no token at all.)
"""

from __future__ import annotations

from typing import List, Literal, Optional

try:  # mcp SDK >= 2.0 renamed the module fastmcp -> mcpserver
    from mcp.server.mcpserver import Context
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import Context

from . import identity
from .config import Config, load
from .embeddings import get_embedder
from .bootstrap import bootstrap_admin
from .schema import migrate
from .store import Store, build_replace


# The MCP `initialize` response carries a server-side `instructions` string; a
# client that honors it (e.g. Claude Code) loads it ONCE at connect, so it guides
# the model without inflating every tool response. Kept small on purpose.
MCP_INSTRUCTION_MAX_BYTES = 2048


def _instruction_text() -> Optional[str]:
    """Operator-supplied MCP instructions from ``MEMGRES_INSTRUCTION``, or None to
    omit the field entirely. Capped at ``MCP_INSTRUCTION_MAX_BYTES`` on a UTF-8
    boundary (clients truncate anyway; we truncate cleanly)."""
    import os
    raw = os.environ.get("MEMGRES_INSTRUCTION", "").strip()
    if not raw:
        return None
    b = raw.encode("utf-8")
    if len(b) <= MCP_INSTRUCTION_MAX_BYTES:
        return raw
    return b[:MCP_INSTRUCTION_MAX_BYTES].decode("utf-8", "ignore")


def _mcp(name: str, instructions: Optional[str] = None):
    # The SDK renamed FastMCP -> MCPServer; support both. ``instructions`` is
    # emitted in the initialize response; None => the SDK omits it.
    kw = {"instructions": instructions} if instructions else {}
    try:
        from mcp.server.mcpserver import MCPServer
        return MCPServer(name, **kw)
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        return FastMCP(name, **kw)


def _mem(m) -> dict:
    return m.to_dict(stringify_dates=True)      # MCP layer needs plain strings


def build_server(cfg: Optional[Config] = None):
    cfg = cfg or load()
    embedder = get_embedder(cfg)
    # A connection pool (not one shared connection) so the Streamable-HTTP
    # transport can serve concurrent clients without interleaving transactions
    # on one libpq handle. min_size=1 keeps it light; MEMGRES_POOL_SIZE caps it.
    # For stdio (one client, sequential calls) the pool simply stays at size 1.
    from psycopg_pool import ConnectionPool
    pool = ConnectionPool(cfg.database_url or "", min_size=1,
                          max_size=cfg.pool_size, open=True)
    with pool.connection() as conn:
        migrate(conn, cfg)
        bootstrap_admin(conn, cfg)      # seed first service admin once (managed)
    # Start the in-process embed worker (if warranted) and set cfg.embed_dispatch
    # to match, so writes defer to it. Kept alive by its own daemon thread.
    from .embed_worker import wire_server
    _worker, cfg, backend = wire_server(cfg, embedder)
    mcp = _mcp("memgres", instructions=_instruction_text())

    # Should the LLM-facing tools carry a `token` argument at all?
    #   MEMGRES_MCP_TOKEN_ARG = on | off | auto (default).
    # auto: expose it only on a genuinely multi-tenant endpoint with no pinned
    # default token — i.e. when the LLM really must supply the identity. When the
    # identity is pinned (MEMGRES_TOKEN, or a per-client Authorization header) or
    # in single mode, the argument is pruned so the agent never sees or fills it.
    import os as _os
    _arg = _os.environ.get("MEMGRES_MCP_TOKEN_ARG", "auto").strip().lower()
    if _arg in ("1", "true", "on", "yes"):
        expose_token = True
    elif _arg in ("0", "false", "off", "no"):
        expose_token = False
    else:
        expose_token = cfg.key_mode != "single" and not cfg.default_token

    def _store(conn):
        return Store(cfg, embedder=embedder, conn=conn, backend=backend)

    def _token(ctx, arg: Optional[str] = None) -> Optional[str]:
        """The caller's token, resolved **authoritatively** so a pin can't be
        overridden by the model:

          1. a per-client ``Authorization: Bearer`` / ``X-Memgres-Token`` header
             (http transport) — how a client pins its own identity;
          2. else ``MEMGRES_TOKEN`` — a pinned default the arg cannot override;
          3. else the explicit ``arg`` — only meaningful on an unpinned,
             multi-tenant endpoint (where the arg is exposed).

        Security does not depend on the schema-pruning below: even if the `token`
        argument stays visible, a header/env pin still wins here."""
        req = None
        if ctx is not None:
            try:
                req = ctx.request_context.request
            except Exception:
                req = None
        if req is not None:
            try:
                tok = identity.bearer_token(req.headers.get("authorization"),
                                            req.headers.get("x-memgres-token"))
                if tok:
                    return tok
            except Exception:
                pass
        return cfg.default_token or arg or None

    def _uid(conn, token: Optional[str]) -> str:
        """Resolve a token to its user id (for read-level identity tools)."""
        p = identity.resolve(conn, cfg, token)
        if p.user_id is None:
            raise identity.AuthError("this token has no owning user")
        return p.user_id

    def _admin_uid(conn, token: Optional[str]) -> str:
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
                     replace_old: Optional[str] = None,
                     replace_new: Optional[str] = None, replace_all: bool = False,
                     path: Optional[str] = None, tags: Optional[List[str]] = None,
                     title: Optional[str] = None,
                     source: Optional[str] = None, reason: Optional[str] = None,
                     ttl_days: Optional[int] = None,
                     space: Optional[str] = None, space_id: Optional[str] = None,
                     token: Optional[str] = None, ctx: Context = None) -> dict:
        """Create or edit a memory. Omit `id` to create (needs `body`). To edit,
        pass `id` plus ONE of: a whole new `body`; a substring edit
        `replace_old`→`replace_new` (server finds `replace_old` and rewrites just
        it — no diff to hand-build, and a body larger than the write cap stays
        editable since only old+new are sent; `replace_old` must be unique unless
        `replace_all=true`); or a unified `diff` with the `base_hash` it was cut
        from. `path`/`tags` set the tree position and labels; `title` is a short
        curated caption (set whole, searchable via `memory_find`); `source`/`reason`
        record provenance. `space` picks one of your namespaces by name (`space_id`
        for a shared one); omit both to use your default."""
        replace = build_replace(replace_old, replace_new)
        with pool.connection() as conn:
            return _mem(_store(conn).write(
                _token(ctx, token), id=id or None, body=body, diff=diff,
                base_hash=base_hash, replace=replace, replace_all=replace_all,
                path=path, tags=tags, title=title, source=source,
                reason=reason, ttl_days=ttl_days, space=space, space_id=space_id))

    @mcp.tool()
    def memory_get(id: str, space: Optional[str] = None,
                   space_id: Optional[str] = None,
                   token: Optional[str] = None, ctx: Context = None) -> dict:
        """Fetch one memory by id (renews its TTL)."""
        with pool.connection() as conn:
            return _mem(_store(conn).get(_token(ctx, token), id,
                                         space=space, space_id=space_id))

    @mcp.tool()
    def memory_recall(query: str, k: int = 10,
                      mode: Literal["lexical", "semantic", "hybrid", "auto"] = "auto",
                      match: Optional[Literal["any", "all"]] = None,
                      tags: Optional[List[str]] = None,
                      path_prefix: Optional[str] = None,
                      snippet: Optional[bool] = None,
                      full_body: Optional[bool] = None,
                      space: Optional[str] = None, space_id: Optional[str] = None,
                      token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """Search memories. `mode`: lexical | semantic | hybrid | auto. `match`
        governs lexical word combination — defaults to OR-any (any query word
        matches, forgiving recall); set 'all' to require every word (narrow).
        Optionally scope to a tag set (`tags`) or a subtree (`path_prefix`, e.g.
        'ops.postgres'). Each hit carries a `snippet` plus `kind` and `lines`:
        `kind="snippet"` is the most relevant slice (semantic/hybrid pick the
        best-matching segment, lexical uses ts_headline) with `lines`=[start,end];
        `kind="full"` means the snippet IS the whole body (short body, or
        `full_body=true`). Pass `full_body=true` to force whole bodies,
        `snippet=false` to skip slicing. `space`/`space_id` pick which namespace
        to search (default: yours)."""
        with pool.connection() as conn:
            return [h.to_recall_dict()
                    for h in _store(conn).recall(
                        _token(ctx, token), query, k=k, tags=tags,
                        path_prefix=path_prefix, mode=mode, match=match,
                        snippet=snippet, full_body=full_body,
                        space=space, space_id=space_id)]

    @mcp.tool()
    def memory_list(path_prefix: Optional[str] = None,
                    tags: Optional[List[str]] = None, limit: int = 50,
                    offset: int = 0, space: Optional[str] = None,
                    space_id: Optional[str] = None,
                    token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """BROWSE (enumerate) a subtree — NOT a search. Lists memories under
        `path_prefix` (e.g. survey all of 'decisions.*') ordered by path, with a
        short first-line `preview` of each. No query, no ranking; use
        `memory_recall` when you want relevance search. Optionally narrow by
        `tags`; `limit`/`offset` paginate. `space`/`space_id` pick the namespace
        (default: yours)."""
        with pool.connection() as conn:
            return _store(conn).list(
                _token(ctx, token), path_prefix=path_prefix, tags=tags,
                limit=limit, offset=offset, space=space, space_id=space_id)

    @mcp.tool()
    def memory_find(query: str, k: int = 10, tags: Optional[List[str]] = None,
                    path_prefix: Optional[str] = None,
                    match: Optional[Literal["any", "all"]] = None,
                    space: Optional[str] = None, space_id: Optional[str] = None,
                    token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """LOCATE by curated `title` (+ tags) — a light "where is it" search over
        titles only, NEVER the body. Returns {id, path, title, tags, score} (no
        body/snippet), so it's cheap to scan before a heavier `memory_recall`.
        Works even without an embedder. Narrow by `tags`/`path_prefix`."""
        with pool.connection() as conn:
            return _store(conn).find(_token(ctx, token), query, k=k, tags=tags,
                                     path_prefix=path_prefix, match=match,
                                     space=space, space_id=space_id)

    @mcp.tool()
    def memory_server_info(ctx: Context = None) -> dict:
        """The server's version + schema_version and its effective limits and
        capabilities (write ceilings, embed provider/model/dim, available recall
        modes, vector backend, key mode, FTS language). Non-sensitive config only —
        no secrets. Read it once so you aren't guessing the version or the limits."""
        from .info import server_info
        dim = embedder.dim if embedder is not None else None
        return server_info(cfg, embed_dim=dim)

    @mcp.tool()
    def memory_blame(id: str, grouped: bool = True,
                     space: Optional[str] = None, space_id: Optional[str] = None,
                     token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """Who last changed each line. Grouped into author-blocks by default;
        set grouped=false for per-line attribution. Each entry carries the
        server-stamped author (author_user_id / author_name) alongside
        seq/op/source/reason — in a shared namespace this says who, authoritatively."""
        with pool.connection() as conn:
            s = _store(conn)
            tok = _token(ctx, token)
            if grouped:
                return s.annotate_grouped(tok, id, space=space, space_id=space_id)
            return s.annotate(tok, id, space=space, space_id=space_id)

    @mcp.tool()
    def memory_history(id: str, space: Optional[str] = None,
                       space_id: Optional[str] = None,
                       token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """The full change chain for a memory: per version the diff, provenance
        (source/reason), the server-stamped author (author_user_id / author_token_id
        / resolved author_name), and the hash-chain fields. Author is authoritative
        (from the authenticated principal), unlike free-text source/reason."""
        with pool.connection() as conn:
            return _store(conn).history(_token(ctx, token), id,
                                        space=space, space_id=space_id)

    @mcp.tool()
    def memory_move(id: str, new_path: str, reason: Optional[str] = None,
                    space: Optional[str] = None, space_id: Optional[str] = None,
                    token: Optional[str] = None, ctx: Context = None) -> dict:
        """Move a memory to a new tree path (cascades its subtree)."""
        with pool.connection() as conn:
            return _mem(_store(conn).move(_token(ctx, token), id, new_path,
                                          reason=reason, space=space,
                                          space_id=space_id))

    @mcp.tool()
    def memory_forget(id: str, space: Optional[str] = None,
                      space_id: Optional[str] = None,
                      token: Optional[str] = None, ctx: Context = None) -> dict:
        """Permanently delete a memory and its history (GDPR erasure)."""
        with pool.connection() as conn:
            return {"forgotten": _store(conn).forget(
                _token(ctx, token), id, space=space, space_id=space_id)}

    # ─── identity: spaces & tokens (open/managed modes) ─────────────────────
    @mcp.tool()
    def memory_list_spaces(token: Optional[str] = None,
                           ctx: Context = None) -> List[dict]:
        """List the namespaces you can reach — your own plus any shared with you —
        with each one's id, name, description, permission and whether it's your
        default. Use a returned `id` as `space_id` to target a shared space; use
        your own space's `name` as `space`."""
        with pool.connection() as conn, conn.transaction():
            return identity.list_spaces(conn, _uid(conn, _token(ctx, token)))

    @mcp.tool()
    def memory_issue_token(permission: str = "write", space: Optional[str] = None,
                           space_id: Optional[str] = None, label: str = "",
                           expires_days: Optional[int] = None,
                           token: Optional[str] = None, ctx: Context = None) -> dict:
        """Mint a new token for your account (rotate / delegate / time-box).
        `permission` is its ceiling (read|write|admin); `space`/`space_id` scope
        it to one namespace (omit for all yours). The secret is returned ONCE."""
        import datetime as dt
        exp = None
        if expires_days:
            exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expires_days)
        with pool.connection() as conn, conn.transaction():
            uid = _admin_uid(conn, _token(ctx, token))
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
    def memory_list_tokens(token: Optional[str] = None,
                           ctx: Context = None) -> List[dict]:
        """List your tokens (metadata only — never the secret)."""
        with pool.connection() as conn, conn.transaction():
            out = identity.list_tokens(conn, _admin_uid(conn, _token(ctx, token)))
        for d in out:                                  # make timestamps JSON-safe
            for key in ("expires_at", "revoked_at", "last_used_at", "created_at"):
                d[key] = str(d[key]) if d[key] else None
        return out

    @mcp.tool()
    def memory_revoke_token(token_id: str, token: Optional[str] = None,
                            ctx: Context = None) -> dict:
        """Revoke one of your tokens by id (kills it immediately)."""
        with pool.connection() as conn, conn.transaction():
            uid = _admin_uid(conn, _token(ctx, token))
            owned = {t["id"] for t in identity.list_tokens(conn, uid)}
            if token_id not in owned:
                raise identity.AuthError("not your token")
            return {"revoked": identity.revoke_token(conn, token_id)}

    # Best-effort: on a pinned/single endpoint drop the (now unused) `token` arg
    # from each tool's advertised schema so the model doesn't even see it. Purely
    # cosmetic — _token above already resolves identity from the header/env and a
    # pin wins regardless; if a future FastMCP changes these internals this simply
    # no-ops and the optional field stays in place (still safe).
    if not expose_token:
        for _t in getattr(getattr(mcp, "_tool_manager", None), "_tools", {}).values():
            _params = getattr(_t, "parameters", None)
            if not isinstance(_params, dict):
                continue
            _params.get("properties", {}).pop("token", None)
            _req = _params.get("required")
            if isinstance(_req, list) and "token" in _req:
                _req.remove("token")

    # Best-effort: with no embedder configured there is no vector backend, so
    # semantic/hybrid recall can't run — drop them from memory_recall's `mode`
    # enum so the model isn't offered modes that will only ever error. Purely
    # cosmetic (same defensive style as the token pruning above): recall()'s
    # backstop still raises on semantic-without-backend, and `lexical`/`auto`
    # (auto resolves to lexical here) stay. Missing keys simply no-op.
    if cfg.embed_provider == "none":
        for _t in getattr(getattr(mcp, "_tool_manager", None), "_tools", {}).values():
            if getattr(_t, "name", None) != "memory_recall":
                continue
            _params = getattr(_t, "parameters", None)
            if not isinstance(_params, dict):
                continue
            _mode = _params.get("properties", {}).get("mode")
            if not isinstance(_mode, dict):
                continue
            _enum = _mode.get("enum")
            if isinstance(_enum, list):
                _mode["enum"] = [m for m in _enum if m not in ("semantic", "hybrid")]

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
