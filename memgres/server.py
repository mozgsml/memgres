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

The namespace token (when MEMGRES_NAMESPACES is on) comes in as a bearer token or
`X-Memgres-Token` header; recall/read are the cheap ops, writes the expensive ones
— the natural place for per-route pricing/metering later.

Concurrency: a psycopg_pool hands each request its own connection; the embedder
is built once and shared. Requires the `[server]` extra (fastapi, uvicorn,
psycopg_pool).
"""

from typing import List, Optional

from .config import Config, load
from .diffing import DiffConflict
from .embeddings import get_embedder
from .identity import SpaceNotFound
from .schema import migrate
from .store import Conflict, NoParent, NotFound, Store, TooLarge


def _parse_lines(spec: Optional[str]) -> Optional[List[int]]:
    """Parse a line selector like '2' or '1,3-5' into [2] / [1,3,4,5]. None → all."""
    if not spec:
        return None
    out: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out or None


def create_app(cfg: Optional[Config] = None):
    from contextlib import asynccontextmanager

    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from psycopg_pool import ConnectionPool
    from pydantic import BaseModel

    cfg = cfg or load()
    embedder = get_embedder(cfg)
    pool = ConnectionPool(cfg.database_url or "", min_size=1, open=False)

    @asynccontextmanager
    async def lifespan(app):
        pool.open()
        with pool.connection() as conn:
            migrate(conn, cfg)          # idempotent; stamps embed model/dim
        yield
        pool.close()

    app = FastAPI(title="memgres", version="0.1.0", lifespan=lifespan)

    # ─── request bodies ─────────────────────────────────────────────────────
    class CreateBody(BaseModel):
        body: str
        path: Optional[str] = None
        tags: Optional[List[str]] = None
        source: Optional[str] = None
        reason: Optional[str] = None
        ttl_days: Optional[int] = None
        space: Optional[str] = None          # namespace name (your own)
        space_id: Optional[str] = None       # namespace id (canonical; shared spaces)

    class EditBody(BaseModel):
        body: Optional[str] = None
        diff: Optional[str] = None
        base_hash: Optional[str] = None
        path: Optional[str] = None
        tags: Optional[List[str]] = None
        source: Optional[str] = None
        reason: Optional[str] = None
        ttl_days: Optional[int] = None
        space: Optional[str] = None
        space_id: Optional[str] = None

    class MoveBody(BaseModel):
        path: str
        source: Optional[str] = None
        reason: Optional[str] = None
        space: Optional[str] = None
        space_id: Optional[str] = None

    # ─── auth: extract the namespace token ──────────────────────────────────
    def token(authorization: Optional[str] = Header(None),
              x_memgres_token: Optional[str] = Header(None)) -> Optional[str]:
        tok = x_memgres_token
        if not tok and authorization and authorization.lower().startswith("bearer "):
            tok = authorization[7:]
        tok = tok or cfg.token          # env default (single-tenant deployments)
        auth_required = cfg.namespaces_enabled or cfg.key_mode != "single"
        if auth_required and not tok:
            raise HTTPException(401, "token required")
        return tok

    def _mem(m) -> dict:
        return {"id": m.id, "content_hash": m.content_hash, "body": m.body,
                "tags": m.tags, "path": m.path, "seq": m.seq,
                "created_at": m.created_at, "updated_at": m.updated_at,
                "expires_at": m.expires_at}

    def _store(conn):
        return Store(cfg, embedder=embedder, conn=conn)

    def _guard(fn):
        """Run a store call, mapping store exceptions to HTTP codes."""
        try:
            return fn()
        except (NotFound, SpaceNotFound):
            raise HTTPException(404, "not found")
        except (Conflict, DiffConflict) as e:
            raise HTTPException(409, str(e))
        except NoParent as e:
            raise HTTPException(409, str(e))
        except TooLarge as e:
            raise HTTPException(413, str(e))
        except PermissionError as e:
            raise HTTPException(401, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    # ─── routes ─────────────────────────────────────────────────────────────
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/memories", status_code=201)
    def create(req: CreateBody, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            m = _guard(lambda: _store(conn).write(
                tok, body=req.body, path=req.path, tags=req.tags,
                source=req.source, reason=req.reason, ttl_days=req.ttl_days,
                space=req.space, space_id=req.space_id))
            return _mem(m)

    @app.get("/memories/{mid}")
    def read(mid: str, tok: Optional[str] = Depends(token),
             space: Optional[str] = None, space_id: Optional[str] = None):
        with pool.connection() as conn:
            return _mem(_guard(lambda: _store(conn).get(
                tok, mid, space=space, space_id=space_id)))

    @app.patch("/memories/{mid}")
    def edit(mid: str, req: EditBody, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            m = _guard(lambda: _store(conn).write(
                tok, id=mid, body=req.body, diff=req.diff, base_hash=req.base_hash,
                path=req.path, tags=req.tags, source=req.source, reason=req.reason,
                ttl_days=req.ttl_days, space=req.space, space_id=req.space_id))
            return _mem(m)

    @app.post("/memories/{mid}/move")
    def move(mid: str, req: MoveBody, tok: Optional[str] = Depends(token)):
        with pool.connection() as conn:
            m = _guard(lambda: _store(conn).move(
                tok, mid, req.path, source=req.source, reason=req.reason,
                space=req.space, space_id=req.space_id))
            return _mem(m)

    @app.delete("/memories/{mid}", status_code=204)
    def forget(mid: str, tok: Optional[str] = Depends(token),
               space: Optional[str] = None, space_id: Optional[str] = None):
        with pool.connection() as conn:
            ok = _guard(lambda: _store(conn).forget(
                tok, mid, space=space, space_id=space_id))
            if not ok:
                raise HTTPException(404, "not found")

    @app.get("/memories/{mid}/history")
    def history(mid: str, tok: Optional[str] = Depends(token),
                space: Optional[str] = None, space_id: Optional[str] = None):
        with pool.connection() as conn:
            return _guard(lambda: _store(conn).history(
                tok, mid, space=space, space_id=space_id))

    @app.get("/memories/{mid}/blame")
    def blame(mid: str, upto_seq: Optional[int] = None,
              group: bool = True, text: bool = True,
              lines: Optional[str] = Query(None, description="e.g. '2' or '1,3-5'"),
              space: Optional[str] = None, space_id: Optional[str] = None,
              tok: Optional[str] = Depends(token)):
        """Who last changed each line. Grouped into author-blocks by default
        (`group=false` for per-line); `text=false` drops bodies for a pure
        ownership map; `lines` selects specific 1-based lines/ranges (per-line)."""
        want = _parse_lines(lines)
        with pool.connection() as conn:
            s = _store(conn)
            if want is not None or not group:
                return _guard(lambda: s.annotate(
                    tok, mid, upto_seq, want, space=space, space_id=space_id))
            return _guard(lambda: s.annotate_grouped(
                tok, mid, upto_seq, text, space=space, space_id=space_id))

    @app.get("/memories/{mid}/at/{seq}")
    def at_version(mid: str, seq: int, tok: Optional[str] = Depends(token),
                   space: Optional[str] = None, space_id: Optional[str] = None):
        """The exact body as it was at version `seq` (reconstructed from history)."""
        with pool.connection() as conn:
            body = _guard(lambda: _store(conn).reconstruct(
                tok, mid, seq, space=space, space_id=space_id))
            return {"seq": seq, "body": body}

    @app.get("/recall")
    def recall(q: str, k: int = 10, mode: str = "auto",
               tags: Optional[str] = Query(None, description="comma-separated"),
               path_prefix: Optional[str] = None,
               space: Optional[str] = None, space_id: Optional[str] = None,
               tok: Optional[str] = Depends(token)):
        taglist = [t for t in (tags.split(",") if tags else []) if t]
        with pool.connection() as conn:
            hits = _guard(lambda: _store(conn).recall(
                tok, q, k=k, tags=taglist or None, path_prefix=path_prefix,
                mode=mode, space=space, space_id=space_id))
            return [{"id": h.id, "body": h.body, "tags": h.tags,
                     "path": h.path, "score": h.score} for h in hits]

    # ─── spaces: what this token can reach ──────────────────────────────────
    @app.get("/spaces")
    def spaces(tok: Optional[str] = Depends(token)):
        """List the namespaces this token can reach (identity modes only)."""
        if cfg.key_mode == "single":
            return []
        from . import identity
        with pool.connection() as conn:
            def _list():
                p = identity.resolve(conn, cfg, tok)
                if p.user_id is None:      # admin or provisional → nothing to list
                    return []
                return identity.list_spaces(conn, p.user_id)
            return _guard(_list)

    return app


def main():  # pragma: no cover - entrypoint
    import os
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get("MEMGRES_HTTP_HOST", "0.0.0.0"),
                port=int(os.environ.get("MEMGRES_HTTP_PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    main()
