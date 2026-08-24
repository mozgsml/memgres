"""Thin HTTP layer over the store (FastAPI).

Deliberately a straight mapping from store operations to REST, so an auth or
billing layer can wrap these routes without touching store logic:

    POST   /memories                create
    GET    /memories/{id}           read (renews TTL)
    PATCH  /memories/{id}           edit: whole body OR diff+base_hash; move; retag
    POST   /memories/{id}/move      convenience reparent
    DELETE /memories/{id}           forget (hard-erase + history)
    GET    /memories/{id}/history   provenance chain
    GET    /recall                  lexical / semantic / hybrid recall
    GET    /healthz                 liveness

In identity modes (MEMGRES_KEY_MODE=open|managed) the token comes in as a bearer
token or `X-Memgres-Token` header; recall/read are the cheap ops, writes the
expensive ones — the natural place for per-route pricing/metering later.

Concurrency: a psycopg_pool hands each request its own connection; the embedder
is built once and shared. Requires the `[server]` extra (fastapi, uvicorn,
psycopg_pool).
"""

import uuid
from typing import List, Optional

import psycopg

from . import admin, identity
from .config import Config, load
from .diffing import DiffConflict
from .embeddings import get_embedder
from .identity import SpaceNotFound
from .lines import parse_line_spec
from .bootstrap import bootstrap_admin
from .schema import migrate
from .store import (Conflict, NoParent, NotFound, PathMoved, PathTaken, Store,
                    TooLarge, build_replace, fold_replace_aliases)


def create_app(cfg: Optional[Config] = None):
    from contextlib import asynccontextmanager

    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from psycopg_pool import ConnectionPool
    from pydantic import BaseModel

    cfg = cfg or load()
    embedder = get_embedder(cfg)
    # Migrate up front (before the worker or any request touches the schema),
    # then start the embed worker and set cfg.embed_dispatch — so the route
    # closures below capture the finalized cfg.
    with psycopg.connect(cfg.database_url or "") as _mc:
        migrate(_mc, cfg)               # idempotent; stamps embed model/dim
        bootstrap_admin(_mc, cfg)       # seed first service admin once (managed)
    from .embed_worker import wire_server
    _worker, cfg, backend = wire_server(cfg, embedder)
    pool = ConnectionPool(cfg.database_url or "", min_size=1,
                          max_size=cfg.pool_size, open=False)

    @asynccontextmanager
    async def lifespan(app):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="memgres", version="0.2.0", lifespan=lifespan)

    # ─── request bodies ─────────────────────────────────────────────────────
    class CreateBody(BaseModel):
        body: str
        path: Optional[str] = None
        tags: Optional[List[str]] = None
        title: Optional[str] = None
        source: Optional[str] = None
        reason: Optional[str] = None
        ttl_days: Optional[int] = None
        space: Optional[str] = None          # namespace name (your own)
        space_id: Optional[str] = None       # namespace id (canonical; shared spaces)
        if_moved: str = "error"              # a vacated `path`: refuse, or "create"

    class EditBody(BaseModel):
        body: Optional[str] = None
        diff: Optional[str] = None
        base_hash: Optional[str] = None
        replace_old: Optional[str] = None
        replace_new: Optional[str] = None
        replace_all: bool = False
        # the spellings file editors use; folded to the canon, conflicts refused
        old_string: Optional[str] = None
        new_string: Optional[str] = None
        old_str: Optional[str] = None
        new_str: Optional[str] = None
        path: Optional[str] = None
        tags: Optional[List[str]] = None
        title: Optional[str] = None
        source: Optional[str] = None
        reason: Optional[str] = None
        ttl_days: Optional[int] = None
        space: Optional[str] = None
        space_id: Optional[str] = None
        if_moved: str = "error"              # a stale address: refuse, or "follow"

    class MoveBody(BaseModel):
        path: str
        source: Optional[str] = None
        reason: Optional[str] = None
        space: Optional[str] = None
        space_id: Optional[str] = None
        if_moved: str = "error"

    # ─── auth: extract the namespace token ──────────────────────────────────
    def token(authorization: Optional[str] = Header(None),
              x_memgres_token: Optional[str] = Header(None)) -> Optional[str]:
        tok = identity.bearer_token(authorization, x_memgres_token)
        tok = tok or cfg.default_token          # env default (single-tenant deployments)
        if cfg.key_mode != "single" and not tok:
            raise HTTPException(401, "token required")
        return tok

    def _mem(m) -> dict:
        return m.to_dict()          # FastAPI JSON-encodes the raw datetimes

    def _store(conn):
        return Store(cfg, embedder=embedder, conn=conn, backend=backend)

    def _guard(fn):
        """Run a store or admin call, mapping domain exceptions to HTTP codes."""
        try:
            return fn()
        except (NotFound, SpaceNotFound):
            raise HTTPException(404, "not found")
        except (Conflict, DiffConflict) as e:
            raise HTTPException(409, str(e))
        except (PathMoved, PathTaken) as e:
            # 409, not 422: the request is well-formed — the caller's picture of
            # where things live is what is out of date. Listed before ValueError,
            # which they subclass.
            raise HTTPException(409, str(e))
        except NoParent as e:
            raise HTTPException(409, str(e))
        except admin.Lockout as e:           # would leave nobody in charge
            raise HTTPException(409, str(e))
        except TooLarge as e:
            raise HTTPException(413, str(e))
        except admin.Forbidden as e:         # before PermissionError — it is one
            raise HTTPException(403, str(e))
        except PermissionError as e:
            raise HTTPException(401, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except psycopg.Error:
            # malformed id (non-uuid space_id), FK violation on a non-existent
            # namespace/request, etc. — a client input error, not a 500. Don't
            # echo the DB message (avoids leaking schema / existence detail).
            raise HTTPException(400, "bad request")

    def _folded_replace(req):
        """The substring edit, whichever of its three spellings arrived."""
        folded = fold_replace_aliases({
            "replace_old": req.replace_old, "replace_new": req.replace_new,
            "old_string": req.old_string, "new_string": req.new_string,
            "old_str": req.old_str, "new_str": req.new_str})
        return build_replace(folded.get("replace_old"), folded.get("replace_new"))

    def _ref(mid: str) -> dict:
        """Turn the URL's memory segment into the store's address argument.

        It may be a memory's uuid or its tree path, so one route set serves both
        instead of a parallel `/by-path/...` tree that would have to be kept in
        step. The test is whether the segment PARSES as a uuid — not whether it
        looks unlike a path. Modern ltree labels accept hyphens and non-ASCII
        (verified on PG 17), so `ops.rate-limits` is a perfectly ordinary path and
        any "contains a dash ⇒ it's an id" shortcut would send it down the wrong
        branch and answer 400.

        A path that is itself uuid-shaped would be read as an id. That is a
        deliberate, fixed precedence rather than a guess that changes with what
        happens to exist.
        """
        try:
            uuid.UUID(mid)
        except ValueError:
            return {"at": mid}
        return {"id": mid}

    # ─── routes ─────────────────────────────────────────────────────────────
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/memories", status_code=201)
    def create(req: CreateBody, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            m = _guard(lambda: _store(conn).write(
                tok, body=req.body, path=req.path, tags=req.tags, title=req.title,
                source=req.source, reason=req.reason, ttl_days=req.ttl_days,
                if_moved=req.if_moved, space=req.space, space_id=req.space_id))
            return _mem(m)

    @app.get("/memories/{mid}")
    def read(mid: str, tok: Optional[str] = Depends(token),
             if_moved: str = "follow", lines: Optional[str] = None,
             space: Optional[str] = None, space_id: Optional[str] = None):
        """Fetch one memory. `mid` is its id or its tree path; a path that has
        since moved is followed by default, and the answer says so via
        `moved_from`. Pass `if_moved=error` to be told instead. `lines`
        ("40-80", "1,10-12") returns only those lines — the answer is then marked
        `partial` and carries no `content_hash`."""
        with pool.connection() as conn:
            return _mem(_guard(lambda: _store(conn).get(
                tok, **_ref(mid), if_moved=if_moved, lines=lines,
                space=space, space_id=space_id)))

    @app.patch("/memories/{mid}")
    def edit(mid: str, req: EditBody, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            # build_replace runs inside _guard so a lone replace_old/new surfaces
            # as its ValueError -> 422, not a silent delete or an uncaught 500.
            m = _guard(lambda: _store(conn).write(
                tok, **_ref(mid), if_moved=req.if_moved,
                body=req.body, diff=req.diff, base_hash=req.base_hash,
                replace=_folded_replace(req),
                replace_all=req.replace_all,
                path=req.path, tags=req.tags, title=req.title,
                source=req.source, reason=req.reason,
                ttl_days=req.ttl_days, space=req.space, space_id=req.space_id))
            return _mem(m)

    @app.post("/memories/{mid}/move")
    def move(mid: str, req: MoveBody, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            m = _guard(lambda: _store(conn).move(
                tok, **_ref(mid), new_path=req.path, if_moved=req.if_moved,
                source=req.source, reason=req.reason,
                space=req.space, space_id=req.space_id))
            return _mem(m)

    @app.delete("/memories/{mid}", status_code=204)
    def forget(mid: str, tok: Optional[str] = Depends(token),
               space: Optional[str] = None, space_id: Optional[str] = None):
        with pool.connection() as conn:
            ok = _guard(lambda: _store(conn).forget(
                tok, **_ref(mid), space=space, space_id=space_id))
            if not ok:
                raise HTTPException(404, "not found")

    @app.get("/memories/{mid}/history")
    def history(mid: str, tok: Optional[str] = Depends(token),
                space: Optional[str] = None, space_id: Optional[str] = None):
        with pool.connection() as conn:
            return _guard(lambda: _store(conn).history(
                tok, **_ref(mid), space=space, space_id=space_id))

    @app.get("/memories/{mid}/blame")
    def blame(mid: str, upto_seq: Optional[int] = None,
              group: bool = True, text: bool = True,
              lines: Optional[str] = Query(None, description="e.g. '2' or '1,3-5'"),
              space: Optional[str] = None, space_id: Optional[str] = None,
              tok: Optional[str] = Depends(token)):
        """Who last changed each line. Grouped into author-blocks by default
        (`group=false` for per-line); `text=false` drops bodies for a pure
        ownership map; `lines` selects specific 1-based lines/ranges (per-line)."""
        # inside _guard: an impossible selector is a 422 about the request, not
        # a 500 about the server
        want = _guard(lambda: parse_line_spec(lines))
        with pool.connection() as conn:
            s = _store(conn)
            if want is not None or not group:
                return _guard(lambda: s.annotate(
                    tok, upto_seq=upto_seq, lines=want, **_ref(mid),
                    space=space, space_id=space_id))
            return _guard(lambda: s.annotate_grouped(
                tok, upto_seq=upto_seq, include_text=text, **_ref(mid),
                space=space, space_id=space_id))

    @app.get("/memories/{mid}/at/{seq}")
    def at_version(mid: str, seq: int, tok: Optional[str] = Depends(token),
                   space: Optional[str] = None, space_id: Optional[str] = None):
        """The exact body as it was at version `seq` (reconstructed from history)."""
        with pool.connection() as conn:
            body = _guard(lambda: _store(conn).reconstruct(
                tok, upto_seq=seq, **_ref(mid), space=space, space_id=space_id))
            return {"seq": seq, "body": body}

    @app.get("/memories")
    def list_memories(path: Optional[str] = None,
                      tags: Optional[str] = Query(None, description="comma-separated"),
                      limit: int = 50, offset: int = 0, bodies: bool = False,
                      space: Optional[List[str]] = Query(
                          None, description="namespace name(s), or 'all'"),
                      space_id: Optional[List[str]] = Query(None),
                      tok: Optional[str] = Depends(token)):
        """Browse (enumerate) a subtree — not a search. Lists memories under
        `path` ordered by path, each with a short first-line `preview`, or with
        whole bodies when `bodies=true` (capped in total; rows past the cap are
        marked `body_omitted`)."""
        taglist = [t for t in (tags.split(",") if tags else []) if t]
        with pool.connection() as conn:
            return _guard(lambda: _store(conn).list(
                tok, path_prefix=path, tags=taglist or None, limit=limit,
                offset=offset, bodies=bodies, space=space, space_id=space_id))

    @app.get("/info")
    def info():
        """Effective server limits + capabilities (non-sensitive config only)."""
        from .info import server_info
        dim = embedder.dim if embedder is not None else None
        return server_info(cfg, embed_dim=dim)

    @app.get("/recall")
    def recall(q: str, k: int = 10, mode: str = "auto",
               tags: Optional[str] = Query(None, description="comma-separated"),
               path_prefix: Optional[str] = None,
               snippet: Optional[bool] = None, full_body: Optional[bool] = None,
               space: Optional[List[str]] = Query(
                   None, description="namespace name(s), or 'all'"),
               space_id: Optional[List[str]] = Query(None),
               tok: Optional[str] = Depends(token)):
        taglist = [t for t in (tags.split(",") if tags else []) if t]
        with pool.connection() as conn:
            hits = _guard(lambda: _store(conn).recall(
                tok, q, k=k, tags=taglist or None, path_prefix=path_prefix,
                mode=mode, snippet=snippet, full_body=full_body,
                space=space, space_id=space_id))
            return [h.to_recall_dict() for h in hits]

    @app.get("/find")
    def find(q: str, k: int = 10,
             tags: Optional[str] = Query(None, description="comma-separated"),
             path_prefix: Optional[str] = None, match: Optional[str] = None,
             space: Optional[List[str]] = Query(
                 None, description="namespace name(s), or 'all'"),
             space_id: Optional[List[str]] = Query(None),
             tok: Optional[str] = Depends(token)):
        """Locate by curated title (+ tags) — light rows, never the body."""
        taglist = [t for t in (tags.split(",") if tags else []) if t]
        with pool.connection() as conn:
            return _guard(lambda: _store(conn).find(
                tok, q, k=k, tags=taglist or None, path_prefix=path_prefix,
                match=match, space=space, space_id=space_id))

    # ─── spaces: what this token can reach ──────────────────────────────────
    @app.get("/spaces")
    def spaces(tok: Optional[str] = Depends(token)):
        """List the namespaces this token can reach (identity modes only)."""
        if cfg.key_mode == "single":
            return []
        with pool.connection() as conn:
            return _guard(lambda: admin.list_spaces(
                conn, identity.resolve(conn, cfg, tok)))

    class NewSpace(BaseModel):
        name: str
        description: str = ""
        instruction: str = ""

    class NewAlias(BaseModel):
        alias: str
        space_id: str

    @app.post("/spaces", status_code=201)
    def create_space(req: NewSpace, tok: Optional[str] = Depends(token)):
        """Create a namespace of your own. Nothing creates one implicitly, so a
        mistyped `space` is an error rather than a new empty space a write
        silently lands in."""
        with pool.connection() as conn, conn.transaction():
            nsid = _guard(lambda: identity.create_own_namespace(
                conn, identity.resolve(conn, cfg, tok), req.name,
                description=req.description, instruction=req.instruction))
        return {"id": nsid, "name": req.name}

    @app.post("/spaces/aliases", status_code=201)
    def set_alias(req: NewAlias, tok: Optional[str] = Depends(token)):
        """Name a reachable namespace for yourself, for when a bare name is
        ambiguous. Private to you, and grants nothing."""
        with pool.connection() as conn, conn.transaction():
            p = identity.resolve(conn, cfg, tok)
            _guard(lambda: identity.create_alias(
                conn, p.user_id, req.alias, req.space_id))
        return {"alias": req.alias, "space_id": req.space_id}

    @app.delete("/spaces/aliases/{alias}", status_code=204)
    def drop_alias(alias: str, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn, conn.transaction():
            p = identity.resolve(conn, cfg, tok)
            if not _guard(lambda: identity.drop_alias(conn, p.user_id, alias)):
                raise HTTPException(404, "not found")

    # ─── request-access: ask to join a namespace, owner approves ────────────
    class RequestBody(BaseModel):
        permission: str = "read"

    @app.post("/spaces/{space_id}/access-requests", status_code=201)
    def request_access(space_id: str, req: RequestBody,
                       tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            return {"id": _guard(lambda: admin.request_access(
                conn, identity.resolve(conn, cfg, tok),
                namespace_id=space_id, permission=req.permission))}

    @app.get("/spaces/{space_id}/access-requests")
    def list_access_requests(space_id: str, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            return _guard(lambda: admin.list_requests(
                conn, identity.resolve(conn, cfg, tok), namespace_id=space_id))

    @app.post("/access-requests/{req_id}/approve")
    def approve_access(req_id: str, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            _guard(lambda: admin.decide_access(
                conn, identity.resolve(conn, cfg, tok),
                request_id=req_id, approve=True))
            return {"approved": req_id}

    @app.post("/access-requests/{req_id}/deny")
    def deny_access(req_id: str, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            _guard(lambda: admin.decide_access(
                conn, identity.resolve(conn, cfg, tok),
                request_id=req_id, approve=False))
            return {"denied": req_id}

    # ─── admin provisioning (service roles + env break-glass) ───────────────
    # The door only says WHO is calling; `admin` decides what they may do, so
    # the same rules serve the MCP surface (and, later, a web session) without
    # being restated here. Authentication failure is 403 rather than 401 on
    # these routes — the credential was offered and rejected.
    def principal(authorization: Optional[str] = Header(None),
                  x_memgres_token: Optional[str] = Header(None)):
        tok = identity.bearer_token(authorization, x_memgres_token)
        with pool.connection() as conn:
            try:
                return identity.resolve(conn, cfg, tok)
            except identity.AuthError as e:
                raise HTTPException(403, str(e))

    class NewUser(BaseModel):
        name: str = ""
        description: str = ""
        role: str = "user"
        can_create_namespace: bool = False
        # who the person is — what makes an authorship line readable
        email: Optional[str] = None
        full_name: Optional[str] = None
        department: Optional[str] = None
        position: Optional[str] = None

    class EditUser(BaseModel):
        name: Optional[str] = None
        description: Optional[str] = None
        email: Optional[str] = None
        full_name: Optional[str] = None
        department: Optional[str] = None
        position: Optional[str] = None

    class NamespaceRight(BaseModel):
        allowed: bool

    class NewNamespace(BaseModel):
        owner_user_id: str
        name: str
        description: str = ""
        instruction: str = ""

    class NewToken(BaseModel):
        user_id: str
        namespace_id: Optional[str] = None
        permission: str = "write"
        label: str = ""
        expires_days: Optional[int] = None

    class NewMember(BaseModel):
        user_id: str
        permission: str = "read"

    @app.post("/admin/users", status_code=201)
    def admin_create_user(req: NewUser, p=Depends(principal)):
        with pool.connection() as conn:
            return {"id": _guard(lambda: admin.create_user(
                conn, p, name=req.name, description=req.description,
                role=req.role,
                can_create_namespace=req.can_create_namespace,
                email=req.email, full_name=req.full_name,
                department=req.department, position=req.position))}

    @app.patch("/admin/users/{user_id}")
    def admin_edit_user(user_id: str, req: EditUser, p=Depends(principal)):
        """Change who a user is. Only the fields sent are touched."""
        with pool.connection() as conn:
            return _guard(lambda: admin.edit_user(
                conn, p, user_id=user_id,
                **req.model_dump(exclude_none=True)))

    @app.post("/admin/users/{user_id}/can-create-namespace")
    def admin_set_create_right(user_id: str, req: NamespaceRight,
                               p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.set_can_create_namespace(
                conn, p, user_id=user_id, allowed=req.allowed))

    @app.get("/whoami")
    def whoami(tok: Optional[str] = Depends(token)):
        """Who this credential is and what it may do — the same capabilities the
        MCP surface reports, so a panel need not re-derive the rules."""
        with pool.connection() as conn:
            return _guard(lambda: admin.whoami(
                conn, identity.resolve(conn, cfg, tok)))

    @app.post("/admin/namespaces", status_code=201)
    def admin_create_namespace(req: NewNamespace, p=Depends(principal)):
        with pool.connection() as conn:
            return {"id": _guard(lambda: admin.create_namespace(
                conn, p, owner_user_id=req.owner_user_id, name=req.name,
                description=req.description, instruction=req.instruction))}

    @app.post("/admin/tokens", status_code=201)
    def admin_issue_token(req: NewToken, p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.issue_token(
                conn, p, user_id=req.user_id, namespace_id=req.namespace_id,
                permission=req.permission, label=req.label,
                expires_days=req.expires_days))

    @app.post("/admin/tokens/{token_id}/revoke")
    def admin_revoke_token(token_id: str, p=Depends(principal)):
        with pool.connection() as conn:
            return {"revoked": _guard(lambda: admin.revoke_token(
                conn, p, token_id=token_id))}

    @app.get("/admin/users/{user_id}/tokens")
    def admin_list_tokens(user_id: str, p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.list_tokens(conn, p, user_id=user_id))

    @app.post("/admin/namespaces/{space_id}/members", status_code=201)
    def admin_add_member(space_id: str, req: NewMember, p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.add_member(
                conn, p, namespace_id=space_id, user_id=req.user_id,
                permission=req.permission))

    class SetRole(BaseModel):
        role: str

    class EditNamespace(BaseModel):
        description: Optional[str] = None
        instruction: Optional[str] = None

    # These exist so the HTTP surface offers what the service layer does. They
    # were reachable over MCP only, which left a panel — the reason this API is
    # public at all — unable to do half the provisioning it shows.
    @app.get("/admin/users")
    def admin_list_users(role: Optional[str] = None, limit: int = 100,
                         offset: int = 0, p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.list_users(conn, p, role=role,
                                                   limit=limit, offset=offset))

    @app.post("/admin/users/{user_id}/role")
    def admin_set_role(user_id: str, req: SetRole, p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.set_role(conn, p, user_id=user_id,
                                                 role=req.role))

    @app.get("/admin/namespaces")
    def admin_list_namespaces(owner_user_id: Optional[str] = None,
                              limit: int = 100, offset: int = 0,
                              p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.list_namespaces(
                conn, p, owner_user_id=owner_user_id, limit=limit, offset=offset))

    @app.patch("/admin/namespaces/{space_id}")
    def admin_edit_namespace(space_id: str, req: EditNamespace,
                             p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.edit_namespace(
                conn, p, namespace_id=space_id, description=req.description,
                instruction=req.instruction))

    @app.get("/admin/namespaces/{space_id}/members")
    def admin_list_members(space_id: str, p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.list_members(conn, p,
                                                     namespace_id=space_id))

    class Adopt(BaseModel):
        namespace_id: str

    @app.get("/admin/orphans")
    def admin_count_orphans(p=Depends(principal)):
        """How many memories are stranded in the pre-identity namespace — the
        only signal a deployment gets that its `single`-mode corpus survived a
        switch to open/managed, since every read of it simply comes back empty."""
        with pool.connection() as conn:
            return _guard(lambda: admin.count_orphans(conn, p))

    @app.post("/admin/adopt-orphans")
    def admin_adopt_orphans(req: Adopt, p=Depends(principal)):
        """Move stranded `single`-mode memories into a real namespace. Idempotent."""
        with pool.connection() as conn, conn.transaction():
            return _guard(lambda: admin.adopt_orphans(
                conn, p, namespace_id=req.namespace_id, vectors=_store(conn)._vectors))

    # ─── service-role management (superadmin only) ──────────────────────────
    @app.post("/admin/users/{user_id}/grant-superadmin")
    def admin_grant_superadmin(user_id: str, p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.grant_superadmin(
                conn, p, user_id=user_id))

    class RevokeSuper(BaseModel):
        demote_to: str = "user"

    @app.post("/admin/users/{user_id}/revoke-superadmin")
    def admin_revoke_superadmin(user_id: str, req: RevokeSuper,
                                p=Depends(principal)):
        with pool.connection() as conn:
            return _guard(lambda: admin.revoke_superadmin(
                conn, p, user_id=user_id, demote_to=req.demote_to))

    return app


def main():  # pragma: no cover - entrypoint
    import os
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get("MEMGRES_HTTP_HOST", "0.0.0.0"),
                port=int(os.environ.get("MEMGRES_HTTP_PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    main()
