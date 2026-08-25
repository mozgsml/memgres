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

from typing import List, Literal, Optional, Union

try:  # mcp SDK >= 2.0 renamed the module fastmcp -> mcpserver
    from mcp.server.mcpserver import Context
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import Context

from . import admin, identity
from .config import Config, load
from .lines import parse_line_spec
from .embeddings import get_embedder
from .bootstrap import bootstrap_admin
from .schema import migrate
from .store import Store, build_replace, fold_replace_aliases

# One namespace, several, or the keyword "all" — the address shape every search
# tool accepts. Named once so the three tools cannot drift apart.
Spaces = Optional[Union[str, List[str]]]


# The MCP `initialize` response carries a server-side `instructions` string; a
# client that honors it (e.g. Claude Code) loads it ONCE at connect, so it guides
# the model without inflating every tool response. Kept small on purpose.
MCP_INSTRUCTION_MAX_BYTES = 2048


# ─── what each tool needs before it is worth showing ─────────────────────────
# Requirements are read against `admin.capabilities()` plus one pseudo-key,
# `identity`, which means "this deployment has identities at all" (every mode
# but `single`). A tool whose requirements are unmet is left out of the list the
# client sees.
#
# **This is not an authorization boundary and must never be mistaken for one.**
# Every tool authorizes on call, against the same service layer the HTTP surface
# uses; a client that ignores the list and calls a hidden tool is refused there.
# What this buys is honesty and context: a read-only agent is not shown five
# write tools it will only ever be refused, and a model that cannot see a tool
# does not spend turns trying it and reasoning about the failure.
#
# A tool missing from this table is SHOWN, not hidden. Hiding by default would
# make forgetting an entry a silent disappearance — the failure that is hard to
# notice — while showing one that cannot be used costs a refusal the caller can
# read. `tests/test_mcp_tool_visibility.py` keeps the table complete anyway.
TOOL_VISIBILITY = {
    # data plane — reads are what any credential can do
    "memory_recall": (),
    "memory_get": (),
    "memory_list": (),
    "memory_tags": (),
    "memory_links": (),
    "memory_blame": (),
    "memory_history": (),
    "memory_server_info": (),
    "memory_write": ("can_write",),
    "memory_move": ("can_write",),
    "memory_forget": ("can_write",),
    # self-service identity — meaningless where there are no identities
    "memory_whoami": ("identity",),
    "memory_list_spaces": ("identity",),
    "memory_set_alias": ("identity",),
    "memory_drop_alias": ("identity",),
    "memory_create_space": ("identity", "can_create_namespace"),
    "memory_issue_token": ("identity", "can_manage_own_tokens"),
    "memory_list_tokens": ("identity", "can_manage_own_tokens"),
    "memory_revoke_token": ("identity", "can_manage_own_tokens"),
    # control plane — the tier each one's service function enforces
    "memory_admin_list_users": ("identity", "can_manage_users"),
    "memory_admin_create_user": ("identity", "can_manage_users"),
    "memory_admin_edit_user": ("identity", "can_manage_users"),
    "memory_admin_set_can_create_namespace": ("identity", "can_manage_users"),
    "memory_admin_list_namespaces": ("identity", "can_manage_users"),
    "memory_admin_create_namespace": ("identity", "can_manage_users"),
    "memory_admin_issue_token": ("identity", "can_manage_users"),
    "memory_admin_list_tokens": ("identity", "can_manage_users"),
    "memory_admin_revoke_token": ("identity", "can_manage_users"),
    "memory_admin_set_role": ("identity", "can_administer_deployment"),
    "memory_admin_add_member": ("identity", "can_administer_deployment"),
    "memory_admin_count_orphans": ("identity", "can_administer_deployment"),
    "memory_admin_adopt_orphans": ("identity", "can_administer_deployment"),
    # per-namespace admin: any namespace OWNER qualifies, so no deployment-wide
    # role decides these — but the credential still has to carry an admin
    # ceiling, since the effective permission is membership ∧ ceiling
    "memory_admin_edit_namespace": ("identity", "has_admin_ceiling"),
    "memory_admin_list_members": ("identity", "has_admin_ceiling"),
}


def visible_tools(names, caps: Optional[dict], identity_on: bool):
    """The subset of ``names`` worth showing to a caller with ``caps``.

    ``caps=None`` means the caller could not be identified (no token, or one
    that failed to resolve). Nothing beyond the unconditional tools is shown
    then — but the read surface stays, so a misconfigured client still sees a
    working server and gets a real authentication error when it calls.
    """
    out = []
    for name in names:
        needs = TOOL_VISIBILITY.get(name)
        if needs is None:                     # unlisted: show it (see above)
            out.append(name)
            continue
        ok = True
        for need in needs:
            if need == "identity":
                ok = identity_on
            else:
                ok = bool(caps and caps.get(need))
            if not ok:
                break
        if ok:
            out.append(name)
    return out


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
    #
    # `version` matters more than it looks: the initialize response is the only
    # thing a client sees BEFORE calling a tool, and it is the first question
    # asked during a coordinated upgrade ("which build is answering?"). It used
    # to go out empty.
    from . import __version__
    kw = {"instructions": instructions} if instructions else {}
    kw["version"] = __version__
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

    # The control-plane tools. `auto` registers them wherever there are
    # identities to administer — that is every mode except `single`, which has
    # exactly one implicit caller and so nothing to administer. Turn them off on
    # an agent-facing endpoint to keep the tool list short; that is a context
    # economy, not a security boundary — each tool authorizes on call.
    _adm = _os.environ.get("MEMGRES_MCP_ADMIN_TOOLS", "auto").strip().lower()
    if _adm in ("1", "true", "on", "yes"):
        admin_surface = True
    elif _adm in ("0", "false", "off", "no"):
        admin_surface = False
    else:
        admin_surface = cfg.key_mode != "single"

    def _store(conn):
        return Store(cfg, embedder=embedder, conn=conn, backend=backend)

    def _iso(rows: List[dict], *keys: str) -> List[dict]:
        """Stringify datetimes in place — MCP returns JSON, FastAPI encodes."""
        for d in rows:
            for k in keys:
                if d.get(k) is not None:
                    d[k] = str(d[k])
        return rows

    _TOKEN_TIMES = ("expires_at", "revoked_at", "last_used_at", "created_at")

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

    def _principal(conn, token: Optional[str]):
        """Authenticate the caller for a control-plane tool.

        The door's whole job: say who is calling. What they may do is `admin`'s
        to decide, with the same rules the HTTP surface uses — which is why no
        permission check appears in this file.
        """
        return identity.resolve(conn, cfg, token)

    @mcp.tool()
    def memory_write(body: Optional[str] = None, id: Optional[str] = None,
                     at: Optional[str] = None,
                     if_moved: Literal["error", "follow", "create"] = "error",
                     diff: Optional[str] = None, base_hash: Optional[str] = None,
                     replace_old: Optional[str] = None,
                     replace_new: Optional[str] = None, replace_all: bool = False,
                     old_string: Optional[str] = None,
                     new_string: Optional[str] = None,
                     old_str: Optional[str] = None,
                     new_str: Optional[str] = None,
                     path: Optional[str] = None, tags: Optional[List[str]] = None,
                     title: Optional[str] = None,
                     source: Optional[str] = None, reason: Optional[str] = None,
                     valid_at: Optional[str] = None,
                     space: Optional[str] = None, space_id: Optional[str] = None,
                     token: Optional[str] = None, ctx: Context = None) -> dict:
        """Create or edit a memory.

        EDIT an existing one by addressing it — `id`, or `at` (the tree path it
        lives at, e.g. at="decisions.pricing") — plus ONE of: a whole new `body`;
        a substring edit `replace_old`→`replace_new` — also accepted as
        `old_string`/`new_string` or `old_str`/`new_str`, the spellings file
        editors use (the server finds `replace_old`
        and rewrites just it — no diff to hand-build, and a body larger than the
        write cap stays editable since only old+new are sent; `replace_old` must
        be unique unless `replace_all=true`); or a unified `diff` with the
        `base_hash` it was cut from.

        CREATE by giving neither `id` nor `at` (needs `body`). `path` files the
        new memory at an address. Note the difference: **`at` finds an existing
        memory, `path` says where a memory lives.** Creating at a path something
        else already occupies is refused, naming the occupant — edit that one with
        `at` instead of making a near-duplicate beside it.

        If the address you used is one a memory MOVED AWAY from, the write is
        refused and the error says where it went — otherwise your edit would land
        on a memory you didn't name, or quietly become a SECOND memory on the same
        subject while the first lives on elsewhere. Then decide: `if_moved="follow"`
        to edit it at its new address, or `if_moved="create"` to claim the vacated
        path for something genuinely new.

        `tags` labels it; `title` is a short curated caption (set whole,
        searchable via `memory_recall`); `source`/`reason` record provenance.
        Link other memories from the body as `[[path]]` (or
        `[[path#anchor|label]]`) — they become a real graph you can walk with
        `memory_links`, including backwards.

        `valid_at` (YYYY-MM-DD) is the day this content was last known to be
        ACCURATE — not the day you wrote it. Set it when the fact comes from a
        dated source ("the letter is from 2021-03") or when you have just
        re-checked one ("still true today"). Omit it and it means "accurate as of
        now", which is the ordinary case; it may point into the past, and that is
        not a mistake. Sending only `valid_at` records a re-confirmation without
        touching the body.
        `space` picks one of your namespaces by name (`space_id` for a shared
        one); omit both when you reach exactly one namespace — with several, say
        which. The answer's `created` says whether
        this made a new memory or edited one."""
        folded = fold_replace_aliases(
            {"replace_old": replace_old, "replace_new": replace_new,
             "old_string": old_string, "new_string": new_string,
             "old_str": old_str, "new_str": new_str})
        replace = build_replace(folded.get("replace_old"),
                                folded.get("replace_new"))
        with pool.connection() as conn:
            return _mem(_store(conn).write(
                _token(ctx, token), id=id or None, at=at or None,
                if_moved=if_moved, body=body, diff=diff,
                base_hash=base_hash, replace=replace, replace_all=replace_all,
                path=path, tags=tags, title=title, source=source,
                reason=reason, valid_at=valid_at, space=space,
                space_id=space_id))

    @mcp.tool()
    def memory_get(id: Optional[str] = None, at: Optional[str] = None,
                   if_moved: Literal["follow", "error"] = "follow",
                   lines: Optional[str] = None,
                   space: Optional[str] = None,
                   space_id: Optional[str] = None,
                   token: Optional[str] = None, ctx: Context = None) -> dict:
        """Fetch one memory, by `id` or by `at` — the tree path it lives at
        (`at="decisions.pricing"`). Renews its TTL. If that path has since moved,
        you get the memory anyway and `moved_from` in the answer tells you the
        address changed — use the new `path` from then on. `if_moved="error"` asks
        to be told rather than redirected.

        `lines` ("40-80", "5", "1,10-12") returns only part of a long body. The
        answer is then marked `partial`, carries `total_lines`, and has NO
        `content_hash` — do not send a slice back as a whole `body`, or
        everything outside it is erased. To change part of a long memory use
        `replace_old`/`replace_new` instead."""
        with pool.connection() as conn:
            return _mem(_store(conn).get(_token(ctx, token), id, at=at,
                                         if_moved=if_moved, lines=lines,
                                         space=space, space_id=space_id))

    @mcp.tool()
    def memory_recall(query: str, k: int = 10,
                      mode: Literal["lexical", "semantic", "hybrid", "auto"] = "auto",
                      match: Optional[Literal["any", "all"]] = None,
                      tags: Optional[List[str]] = None,
                      path_prefix: Optional[str] = None,
                      snippet: Optional[bool] = None,
                      full_body: Optional[bool] = None,
                      bodies: bool = True,
                      match_tags: Optional[Literal["all", "any"]] = None,
                      space: Spaces = None, space_id: Spaces = None,
                      token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """Search memories — bodies AND curated titles, with a title match
        weighted higher. `mode`: lexical | semantic | hybrid | auto. `match`
        governs lexical word combination — defaults to OR-any (any query word
        matches, forgiving recall); set 'all' to require every word (narrow).
        Optionally scope to a tag set (`tags` — `match_tags="all"`, the default,
        needs every one; `"any"` needs at least one) or a subtree (`path_prefix`,
        e.g. 'ops.postgres'). `memory_tags` lists the vocabulary in use, so you
        can reuse an existing tag instead of inventing a near-duplicate. Each hit carries a `snippet` plus `kind` and `lines`:
        `kind="snippet"` is the most relevant slice (semantic/hybrid pick the
        best-matching segment, lexical uses ts_headline) with `lines`=[start,end];
        `kind="full"` means the snippet IS the whole body (short body, or
        `full_body=true`). Pass `full_body=true` to force whole bodies,
        `snippet=false` to skip slicing.

        `bodies=false` is the cheap "where is it" pass: ranked hits carrying
        id/path/title/tags and NO text. Use it to scan a wide result set before
        choosing what to read in full with `memory_get`.

        WHERE to search: `space` takes a namespace name, a list of names, or
        `"all"` for every namespace you reach; `space_id` takes ids (the only way
        to name a namespace shared WITH you). Omit both and your single namespace
        is used — but if you reach several, naming one is REQUIRED, because
        searching just one of them would return "nothing found" and read like an
        answer. Every hit carries `space`/`space_id` saying where it came from."""
        with pool.connection() as conn:
            return [h.to_recall_dict()
                    for h in _store(conn).recall(
                        _token(ctx, token), query, k=k, tags=tags,
                        path_prefix=path_prefix, mode=mode, match=match,
                        snippet=snippet, full_body=full_body, bodies=bodies,
                        match_tags=match_tags, space=space, space_id=space_id)]

    @mcp.tool()
    def memory_list(path_prefix: Optional[str] = None,
                    tags: Optional[List[str]] = None, limit: int = 50,
                    offset: int = 0, bodies: bool = False,
                    match_tags: Optional[Literal["all", "any"]] = None,
                    space: Spaces = None, space_id: Spaces = None,
                    token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """BROWSE (enumerate) a subtree — NOT a search. Lists memories under
        `path_prefix` (e.g. survey all of 'decisions.*') ordered by path, with a
        short first-line `preview` of each. No query, no ranking; use
        `memory_recall` when you want relevance search. Optionally narrow by
        `tags` (`match_tags="all"` needs every one, `"any"` at least one);
        `limit`/`offset` paginate. `bodies=true` returns whole bodies
        instead of previews — read a subtree in ONE call instead of a browse plus
        a fetch per row; it is capped in total, and rows past the cap come back
        with `body_omitted=true` rather than being silently dropped.
        `space`/`space_id` pick the namespace(s) — a name, a list, or `"all"`;
        required when you reach several. Rows carry `space`/`space_id`."""
        with pool.connection() as conn:
            return _store(conn).list(
                _token(ctx, token), path_prefix=path_prefix, tags=tags,
                limit=limit, offset=offset, bodies=bodies,
                match_tags=match_tags, space=space, space_id=space_id)

    @mcp.tool()
    def memory_tags(prefix: Optional[str] = None, k: int = 50,
                    space: Spaces = None, space_id: Spaces = None,
                    token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """The tag vocabulary in use, most-used first: `{tag, count}`.

        READ THIS BEFORE TAGGING A NEW MEMORY. Tags match exactly, so a label you
        invent that already exists in another spelling becomes a second, unrelated
        tag and neither filter finds both. `prefix` narrows (e.g. "ops"), `k`
        caps the list. Tags are stored lowercased and Unicode-normalised, so case
        and accent form are not what makes two tags different — wording is."""
        with pool.connection() as conn:
            return _store(conn).tags(_token(ctx, token), prefix=prefix, k=k,
                                     space=space, space_id=space_id)

    @mcp.tool()
    def memory_links(id: Optional[str] = None, at: Optional[str] = None,
                     direction: Literal["in", "out", "both"] = "both",
                     space: Optional[str] = None, space_id: Optional[str] = None,
                     token: Optional[str] = None, ctx: Context = None) -> dict:
        """What this memory links to, and WHAT LINKS TO IT. Address it by `id` or
        `at` (its path).

        `in` is the half you cannot get by reading the memory: before you change
        a fact, this is who is relying on it. `out` lists its `[[links]]` and is
        where a DANGLING one shows up — `resolved: false` means the target has
        not been written yet, or was erased.

        Write a link as `[[path]]`, or `[[path#anchor|label]]`. Pointers at other
        stores use a scheme (`[[idea:some-slug]]`); anything else in double
        brackets is left as plain text."""
        with pool.connection() as conn:
            return _store(conn).links(_token(ctx, token), id=id or None,
                                      at=at or None, direction=direction,
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
    def memory_blame(id: Optional[str] = None, at: Optional[str] = None,
                     grouped: bool = True, lines: Optional[str] = None,
                     space: Optional[str] = None, space_id: Optional[str] = None,
                     token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """Who last changed each line. `lines` ("2", "1,3-5") narrows it to
        specific lines (per-line attribution). Grouped into author-blocks by default;
        set grouped=false for per-line attribution. Each entry carries the
        server-stamped author (author_user_id / author_name) alongside
        seq/op/source/reason — in a shared namespace this says who, authoritatively."""
        with pool.connection() as conn:
            s = _store(conn)
            tok = _token(ctx, token)
            want = parse_line_spec(lines)
            if grouped and want is None:
                return s.annotate_grouped(tok, id, at=at, space=space,
                                          space_id=space_id)
            return s.annotate(tok, id, lines=want, at=at, space=space,
                              space_id=space_id)

    @mcp.tool()
    def memory_history(id: Optional[str] = None, at: Optional[str] = None,
                       space: Optional[str] = None,
                       space_id: Optional[str] = None,
                       token: Optional[str] = None, ctx: Context = None) -> List[dict]:
        """The full change chain for a memory: per version the diff, provenance
        (source/reason), the server-stamped author (author_user_id / author_token_id
        / resolved author_name), and the hash-chain fields. Author is authoritative
        (from the authenticated principal), unlike free-text source/reason."""
        with pool.connection() as conn:
            return _store(conn).history(_token(ctx, token), id, at=at,
                                        space=space, space_id=space_id)

    @mcp.tool()
    def memory_move(new_path: str, id: Optional[str] = None,
                    at: Optional[str] = None,
                    if_moved: Literal["error", "follow"] = "error",
                    reason: Optional[str] = None,
                    space: Optional[str] = None, space_id: Optional[str] = None,
                    token: Optional[str] = None, ctx: Context = None) -> dict:
        """Move a memory to a new tree path (cascades its subtree). Address it by
        `id` or by `at` (its current path). Every node that moves records the
        move, so its old path still resolves afterwards."""
        with pool.connection() as conn:
            return _mem(_store(conn).move(_token(ctx, token), id, new_path,
                                          at=at, if_moved=if_moved,
                                          reason=reason, space=space,
                                          space_id=space_id))

    @mcp.tool()
    def memory_forget(id: Optional[str] = None, at: Optional[str] = None,
                      space: Optional[str] = None,
                      space_id: Optional[str] = None,
                      token: Optional[str] = None, ctx: Context = None) -> dict:
        """Permanently delete a memory and its history (GDPR erasure). Address it
        by `id` or by `at` (its path). A path that has since moved is REFUSED
        rather than followed — deleting on the strength of a stale address is the
        one mistake here you cannot undo."""
        with pool.connection() as conn:
            return {"forgotten": _store(conn).forget(
                _token(ctx, token), id, at=at, space=space, space_id=space_id)}

    # ─── identity: spaces & tokens (open/managed modes) ─────────────────────
    @mcp.tool()
    def memory_list_spaces(token: Optional[str] = None,
                           ctx: Context = None) -> List[dict]:
        """List the namespaces you can reach — your own plus any shared with you —
        with each one's id, name, description, permission, and your `alias` for it
        if you set one. To address one, pass its `alias` (or `name`, when that
        name is unambiguous for you) as `space`, or its `id` as `space_id`."""
        with pool.connection() as conn, conn.transaction():
            return identity.list_spaces(conn, _uid(conn, _token(ctx, token)))

    @mcp.tool()
    def memory_create_space(name: str, description: str = "",
                            instruction: str = "",
                            token: Optional[str] = None,
                            ctx: Context = None) -> dict:
        """Create a namespace of your own. Nothing creates one implicitly, so a
        mistyped `space` is an error rather than a new empty space your write
        silently lands in. `description` says what belongs here and `instruction`
        tells an agent how to use it. Needs the right to create namespaces."""
        with pool.connection() as conn, conn.transaction():
            nsid = identity.create_own_namespace(
                conn, _principal(conn, _token(ctx, token)), name,
                description=description, instruction=instruction)
        return {"id": nsid, "name": name}

    @mcp.tool()
    def memory_set_alias(alias: str, space_id: str,
                         token: Optional[str] = None,
                         ctx: Context = None) -> dict:
        """Give a namespace a name of your own, for when a bare name is ambiguous
        — two people can each own a 'notes', and once one is shared with you the
        name means two things. An alias is private to you and grants nothing: the
        space must already be reachable. It is refused if the name already
        resolves for you."""
        with pool.connection() as conn, conn.transaction():
            uid = _uid(conn, _token(ctx, token))
            identity.create_alias(conn, uid, alias, space_id)
        return {"alias": alias, "space_id": space_id}

    @mcp.tool()
    def memory_drop_alias(alias: str, token: Optional[str] = None,
                          ctx: Context = None) -> dict:
        """Remove one of your namespace aliases. The namespace itself is
        untouched — an alias is only a name."""
        with pool.connection() as conn, conn.transaction():
            uid = _uid(conn, _token(ctx, token))
            return {"dropped": identity.drop_alias(conn, uid, alias)}

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
            tok = _token(ctx, token)
            uid = _admin_uid(conn, tok)
            nsid = space_id
            if nsid is not None:
                # can only scope a new token to a namespace the caller can reach
                with conn.cursor() as cur:
                    if identity._reach(cur, uid, nsid) is None:
                        raise identity.AuthError(
                            "cannot scope a token to an unreachable namespace")
            elif space is not None:
                # Naming a namespace here used to CREATE it unconditionally,
                # which walked straight past the right that governs creation
                # everywhere else — so a user refused a namespace on the write
                # path could mint one through the token tool and write there.
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM namespace "
                                "WHERE owner_user_id=%s AND name=%s", (uid, space))
                    row = cur.fetchone()
                if row is not None:
                    nsid = str(row[0])
                else:
                    principal = identity.resolve(conn, cfg, tok)
                    if not identity.can_create_namespace(conn, principal):
                        raise identity.AuthError(
                            f"you own no namespace named '{space}' and may not "
                            "create one — ask an admin to create it or share it")
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
        return _iso(out, *_TOKEN_TIMES)

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

    @mcp.tool()
    def memory_whoami(token: Optional[str] = None, ctx: Context = None) -> dict:
        """Who you are and what you may do: user id, service role, this
        credential's permission ceiling and namespace scope, plus the
        capabilities that follow from them. Check here before assuming an admin
        tool will refuse you — or explaining to someone why it did."""
        with pool.connection() as conn:
            return admin.whoami(conn, _principal(conn, _token(ctx, token)))

    # ─── control plane: provisioning (authorized in `admin`, not here) ───────
    if admin_surface:

        @mcp.tool()
        def memory_admin_list_users(role: Optional[str] = None, limit: int = 50,
                                    offset: int = 0, token: Optional[str] = None,
                                    ctx: Context = None) -> List[dict]:
            """The user directory: id, name, service role, profile
            (full_name/email/department/position) and the namespace-creation right.
            `role` filters to one tier (user | user_manager | superadmin)."""
            with pool.connection() as conn:
                out = admin.list_users(conn, _principal(conn, _token(ctx, token)),
                                       role=role, limit=limit, offset=offset)
            return _iso(out, "created_at")

        @mcp.tool()
        def memory_admin_create_user(name: str = "", description: str = "",
                                     role: str = "user",
                                     can_create_namespace: bool = False,
                                     email: Optional[str] = None,
                                     full_name: Optional[str] = None,
                                     department: Optional[str] = None,
                                     position: Optional[str] = None,
                                     token: Optional[str] = None,
                                     ctx: Context = None) -> dict:
            """Create a user and return its id. A new user owns nothing yet —
            give it a namespace with `memory_admin_create_namespace`, share one
            with `memory_admin_add_member`, or set `can_create_namespace` so it
            can make its own. Minting an admin-role user requires superadmin.

            `full_name`/`email`/`department`/`position` say who the person is;
            `full_name` is what `memory_blame` shows as the author, so without it
            an audit trail reads as bare uuids."""
            with pool.connection() as conn, conn.transaction():
                uid = admin.create_user(conn, _principal(conn, _token(ctx, token)),
                                        name=name, description=description,
                                        role=role,
                                        can_create_namespace=can_create_namespace,
                                        email=email, full_name=full_name,
                                        department=department, position=position)
            return {"id": uid}

        @mcp.tool()
        def memory_admin_edit_user(user_id: str,
                                   name: Optional[str] = None,
                                   description: Optional[str] = None,
                                   email: Optional[str] = None,
                                   full_name: Optional[str] = None,
                                   department: Optional[str] = None,
                                   position: Optional[str] = None,
                                   token: Optional[str] = None,
                                   ctx: Context = None) -> dict:
            """Change who a user is. Only the fields you pass are touched, so a
            partial update cannot blank the rest."""
            fields = {"name": name, "description": description, "email": email,
                      "full_name": full_name, "department": department,
                      "position": position}
            with pool.connection() as conn, conn.transaction():
                return admin.edit_user(
                    conn, _principal(conn, _token(ctx, token)), user_id=user_id,
                    **{k: v for k, v in fields.items() if v is not None})

        @mcp.tool()
        def memory_admin_set_can_create_namespace(user_id: str, allowed: bool,
                                                  token: Optional[str] = None,
                                                  ctx: Context = None) -> dict:
            """Grant or withdraw a user's right to create namespaces. Without it
            they must be given a namespace or shared into one; writing to a name
            that does not exist is refused instead of silently making it."""
            with pool.connection() as conn, conn.transaction():
                return admin.set_can_create_namespace(
                    conn, _principal(conn, _token(ctx, token)),
                    user_id=user_id, allowed=allowed)

        @mcp.tool()
        def memory_admin_set_role(user_id: str, role: str,
                                  token: Optional[str] = None,
                                  ctx: Context = None) -> dict:
            """Set a user's service role (user | user_manager | superadmin).
            Superadmin only. Demoting the last superadmin is refused — that
            would leave the deployment with no control plane."""
            with pool.connection() as conn, conn.transaction():
                return admin.set_role(conn, _principal(conn, _token(ctx, token)),
                                      user_id=user_id, role=role)

        @mcp.tool()
        def memory_admin_list_namespaces(owner_user_id: Optional[str] = None,
                                         limit: int = 50, offset: int = 0,
                                         token: Optional[str] = None,
                                         ctx: Context = None) -> List[dict]:
            """Every namespace on the deployment, with its owner and routing
            instruction. `memory_list_spaces` answers what *you* can reach; this
            answers what exists."""
            with pool.connection() as conn:
                out = admin.list_namespaces(
                    conn, _principal(conn, _token(ctx, token)),
                    owner_user_id=owner_user_id, limit=limit, offset=offset)
            return _iso(out, "created_at")

        @mcp.tool()
        def memory_admin_create_namespace(name: str, owner_user_id: str,
                                          description: str = "",
                                          instruction: str = "",
                                          token: Optional[str] = None,
                                          ctx: Context = None) -> dict:
            """Create a namespace owned by `owner_user_id`. `instruction` is the
            routing hint agents read to decide what belongs here — write it as
            guidance, not decoration. Idempotent: an existing name returns it."""
            with pool.connection() as conn, conn.transaction():
                nsid = admin.create_namespace(
                    conn, _principal(conn, _token(ctx, token)),
                    owner_user_id=owner_user_id, name=name,
                    description=description, instruction=instruction)
            return {"id": nsid}

        @mcp.tool()
        def memory_admin_edit_namespace(space_id: str,
                                        description: Optional[str] = None,
                                        instruction: Optional[str] = None,
                                        token: Optional[str] = None,
                                        ctx: Context = None) -> dict:
            """Amend a namespace's description or routing instruction. Creating
            it again will not: that call ignores conflicts, so this is the only
            way to correct the text agents route by."""
            with pool.connection() as conn, conn.transaction():
                return admin.edit_namespace(
                    conn, _principal(conn, _token(ctx, token)),
                    namespace_id=space_id, description=description,
                    instruction=instruction)

        @mcp.tool()
        def memory_admin_count_orphans(token: Optional[str] = None,
                                       ctx: Context = None) -> dict:
            """How many memories are stranded in the pre-identity namespace.
            `single` mode stores everything under one nameless namespace; after a
            switch to open/managed nobody resolves to it, so the old corpus is
            present but invisible and every read of it just comes back empty.
            This is the only thing that says so."""
            with pool.connection() as conn:
                return admin.count_orphans(conn, _principal(conn, _token(ctx, token)))

        @mcp.tool()
        def memory_admin_adopt_orphans(space_id: str,
                                       token: Optional[str] = None,
                                       ctx: Context = None) -> dict:
            """Move every stranded `single`-mode memory into a real namespace.
            Idempotent — with nothing stranded it changes nothing. Moves the chunk
            vectors too, so semantic recall keeps working; leaving them behind
            would make it answer empty without erroring."""
            with pool.connection() as conn, conn.transaction():
                return admin.adopt_orphans(
                    conn, _principal(conn, _token(ctx, token)),
                    namespace_id=space_id, vectors=backend)

        @mcp.tool()
        def memory_admin_add_member(space_id: str, user_id: str,
                                    permission: str = "read",
                                    token: Optional[str] = None,
                                    ctx: Context = None) -> dict:
            """Share a namespace with another user at read | write | admin.
            Superadmin only — it grants access across tenants."""
            with pool.connection() as conn, conn.transaction():
                return admin.add_member(conn, _principal(conn, _token(ctx, token)),
                                        namespace_id=space_id, user_id=user_id,
                                        permission=permission)

        @mcp.tool()
        def memory_admin_list_members(space_id: str,
                                      token: Optional[str] = None,
                                      ctx: Context = None) -> List[dict]:
            """Who can reach a namespace — the owner first, then everyone shared
            in. The audit answer to "who can see this?"."""
            with pool.connection() as conn:
                out = admin.list_members(conn, _principal(conn, _token(ctx, token)),
                                         namespace_id=space_id)
            return _iso(out, "created_at")

        @mcp.tool()
        def memory_admin_issue_token(user_id: str, permission: str = "write",
                                     space_id: Optional[str] = None,
                                     label: str = "",
                                     expires_days: Optional[int] = None,
                                     token: Optional[str] = None,
                                     ctx: Context = None) -> dict:
            """Mint a token for another user. `permission` is its ceiling,
            `space_id` scopes it to one namespace (omit for all theirs). The
            secret is returned ONCE. Issuing for an admin-role account requires
            superadmin."""
            with pool.connection() as conn, conn.transaction():
                return admin.issue_token(
                    conn, _principal(conn, _token(ctx, token)), user_id=user_id,
                    namespace_id=space_id, permission=permission, label=label,
                    expires_days=expires_days)

        @mcp.tool()
        def memory_admin_list_tokens(user_id: str, token: Optional[str] = None,
                                     ctx: Context = None) -> List[dict]:
            """A user's tokens — metadata only, never the secret."""
            with pool.connection() as conn:
                out = admin.list_tokens(conn, _principal(conn, _token(ctx, token)),
                                        user_id=user_id)
            return _iso(out, *_TOKEN_TIMES)

        @mcp.tool()
        def memory_admin_revoke_token(token_id: str,
                                      token: Optional[str] = None,
                                      ctx: Context = None) -> dict:
            """Kill any user's token immediately. False means it was already
            revoked or never existed."""
            with pool.connection() as conn, conn.transaction():
                return {"revoked": admin.revoke_token(
                    conn, _principal(conn, _token(ctx, token)), token_id=token_id)}

    # ─── per-caller tool list ───────────────────────────────────────────────
    # MEMGRES_MCP_TOOL_VISIBILITY = auto (default) | off.
    #
    # The tool list is answered per request, so on an http endpoint each client
    # sees the tools ITS token can use; on stdio the pinned token makes the
    # answer constant. Note it is computed at LIST time: a client caches what it
    # got at connect, so rights changed mid-session are not reflected until it
    # lists again. That is a display lag, never a permission one — the call path
    # re-authorizes every time.
    _vis = _os.environ.get("MEMGRES_MCP_TOOL_VISIBILITY", "auto").strip().lower()
    if _vis not in ("0", "false", "off", "no"):
        _identity_on = cfg.key_mode != "single"
        _all_caps = {k: True for k in
                     ("can_write", "can_create_namespace", "can_manage_users",
                      "can_manage_own_tokens", "can_administer_deployment",
                      "has_admin_ceiling", "is_admin")}

        def _caps_for_caller():
            """This request's capabilities, or None if the caller is unknown.

            Only a failed CREDENTIAL answers None. A database that is briefly
            unavailable raises, and the `tools/list` fails — which a client
            retries. Swallowing it would answer with the read-only subset
            instead: a client lists once at connect and caches, so a one-second
            blip would take an agent's write tools away for the whole session,
            and the agent would report the server as read-only.
            """
            if not _identity_on:
                # One implicit caller who may do everything; there is no
                # credential to resolve and nothing to withhold.
                return _all_caps
            try:
                ctx = mcp.get_context()
            except Exception:
                ctx = None
            with pool.connection() as conn:
                try:
                    # `touch=False`: listing tools is asking ABOUT the
                    # credential, not acting on it. Stamping `last_used_at` here
                    # would make every `tools/list` a write transaction and turn
                    # the column into "last connected".
                    p = identity.resolve(conn, cfg, _token(ctx, None),
                                         touch=False)
                except identity.AuthError:
                    return None
                return admin.capabilities(conn, p)

        def _keep(tools):
            allowed = set(visible_tools([t.name for t in tools],
                                        _caps_for_caller(), _identity_on))
            return [t for t in tools if t.name in allowed]

        # Where the filter attaches differs by SDK generation, so both are
        # written out rather than guessed at:
        #
        #   2.x — `_handle_list_tools` calls `self.list_tools()` when the
        #         request arrives, so replacing that attribute is what the
        #         request actually runs.
        #   1.x — the low-level server captured the bound `list_tools` at
        #         construction, so the attribute is already spoken for and the
        #         handler has to be re-registered instead.
        #
        # 🔴 This used to be one `try: … except Exception: pass`. That is how the
        # feature came to be silently DEAD on mcp 2.x — installed on nothing,
        # every client still seeing every tool, no error anywhere. A guard that
        # turns "this build cannot do what you configured" into silence is worse
        # than the crash it prevents, so an unrecognised SDK now says so.
        if hasattr(mcp, "_handle_list_tools"):              # mcp 2.x
            _unfiltered = mcp.list_tools

            async def _list_visible_tools():
                return _keep(await _unfiltered())

            mcp.list_tools = _list_visible_tools
        elif hasattr(mcp, "_mcp_server"):                   # mcp 1.x
            async def _list_visible_tools():
                return _keep(await mcp.list_tools())

            mcp._mcp_server.list_tools()(_list_visible_tools)
        else:                                               # pragma: no cover
            raise RuntimeError(
                "MEMGRES_MCP_TOOL_VISIBILITY is on, but this mcp SDK exposes "
                "neither hook memgres knows how to filter the tool list with — "
                "so every client would see every tool while the configuration "
                "said otherwise. Set MEMGRES_MCP_TOOL_VISIBILITY=off to accept "
                "that deliberately, or upgrade memgres")

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
        host = os.environ.get("MEMGRES_MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MEMGRES_MCP_PORT", "8765"))
        # mcp SDK 1.x (FastMCP) reads host/port from `settings`; 2.x (MCPServer)
        # dropped those fields and takes them as run() kwargs (forwarded to
        # run_streamable_http_async). Pick the path the installed SDK supports.
        fields = getattr(type(server.settings), "model_fields", {})
        if "host" in fields:
            server.settings.host = host
            server.settings.port = port
            server.run(transport="streamable-http")
        else:
            server.run(transport="streamable-http", host=host, port=port)
    else:
        server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
