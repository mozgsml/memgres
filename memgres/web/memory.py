"""Memory, as the panel sees it: read-only.

Every read goes through the ordinary namespace rules with the session's
read-ceiling principal. The graph is the one read the store has no method for —
a whole space's shape in one request instead of one call per record — so it is
written here, on the same predicates the store uses (``build_filters``).
"""

from typing import Optional

from .. import identity
from ..vector.base import build_filters

# The graph answers with at most this many records. Past it the answer says
# `truncated`, and the panel offers search and the local view instead of
# pretending the space is smaller than it is.
MAX_GRAPH_NODES = 5000
MAX_SEARCH_HITS = 40


def spaces(conn, user_id: str) -> list:
    rows = identity.list_spaces(conn, user_id)
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    where, params = build_filters(ids, None, None)
    with conn.cursor() as cur:
        cur.execute(f"SELECT namespace, count(*) FROM memory WHERE {where} GROUP BY namespace",
                    params)
        counts = {str(ns): n for ns, n in cur.fetchall()}
        # people waiting to join — only where this person decides
        admin_ids = [r["id"] for r in rows if r["permission"] == "admin"]
        waiting = {}
        if admin_ids:
            cur.execute("SELECT namespace_id, count(*) FROM access_request WHERE status = 'pending' "
                        "AND namespace_id = ANY(%s::uuid[]) GROUP BY namespace_id", (admin_ids,))
            waiting = {str(ns): n for ns, n in cur.fetchall()}
    return [{"id": r["id"], "name": r["name"], "description": r["description"],
             "permission": r["permission"], "mine": r["mine"], "alias": r["alias"],
             "records": counts.get(r["id"], 0), "waiting": waiting.get(r["id"], 0)} for r in rows]


def graph(conn, principal, space_id: str) -> dict:
    """Every live record of one space — path, title, tags, when it changed —
    and the links between them. No bodies: the shape, not the content."""
    with conn.transaction():
        nsid, _perm = identity.resolve_space(conn, principal, space_id=space_id)
    where, params = build_filters([nsid], None, None)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM memory WHERE {where}", params)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT id, path::text, title, tags, updated_at FROM memory WHERE {where} "
            f"ORDER BY path NULLS LAST, id LIMIT %s", params + [MAX_GRAPH_NODES])
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        links = []
        if ids:
            cur.execute(
                "SELECT DISTINCT src_id, dst_id FROM memory_link "
                "WHERE src_id = ANY(%s) AND dst_id = ANY(%s) AND src_id <> dst_id",
                (ids, ids))
            links = [{"a": str(a), "b": str(b)} for a, b in cur.fetchall()]
    return {
        "space_id": nsid,
        "total": total,
        "truncated": total > len(rows),
        "records": [{"id": str(i), "path": p, "title": t or "", "tags": list(tags or []),
                     "updated_at": u} for i, p, t, tags, u in rows],
        "links": links,
    }


def mount(app, cfg, pool, panel, make_store) -> None:
    from fastapi import HTTPException, Request

    from ..identity import AuthError, SpaceNotFound
    from ..store import NotFound

    def _reader(request: Request):
        s = panel["session"](request)
        if s.user_id is None:
            raise HTTPException(409, "the administrator token has no memory to show")
        return s, panel["principal"](s)

    def _guard(fn):
        try:
            return fn()
        except (NotFound, SpaceNotFound, KeyError):
            raise HTTPException(404, "not found")
        except AuthError:
            # unreachable reads the same as missing
            raise HTTPException(404, "not found")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/ui/api/spaces")
    def my_spaces(request: Request):
        s, _ = _reader(request)
        with pool.connection() as conn:
            return {"spaces": spaces(conn, s.user_id)}

    @app.get("/ui/api/spaces/{space_id}/graph")
    def space_graph(space_id: str, request: Request):
        _, p = _reader(request)
        with pool.connection() as conn:
            return _guard(lambda: graph(conn, p, identity._as_uuid(space_id)))

    @app.get("/ui/api/spaces/{space_id}/records/{record_id}")
    def record(space_id: str, record_id: str, request: Request):
        _, p = _reader(request)
        with pool.connection() as conn:
            store = make_store(conn)
            sid, rid = _guard(lambda: (identity._as_uuid(space_id), identity._as_uuid(record_id)))
            m = _guard(lambda: store.get(p, id=rid, space_id=sid, _count=False)).to_dict()
            links = _guard(lambda: store.links(p, id=rid, direction="both", space_id=sid))
            return {"record": m, "links": links}

    @app.get("/ui/api/spaces/{space_id}/search")
    def search(space_id: str, q: str, request: Request, k: Optional[int] = None):
        _, p = _reader(request)
        q = (q or "").strip()
        if not q:
            return {"hits": []}
        if len(q) > 500:
            raise HTTPException(422, "the query is longer than 500 characters")
        with pool.connection() as conn:
            sid = _guard(lambda: identity._as_uuid(space_id))
            hits = _guard(lambda: make_store(conn).recall(
                p, q, k=min(k or MAX_SEARCH_HITS, MAX_SEARCH_HITS), space_id=sid,
                bodies=True, snippet=True, _count=False))
            return {"hits": [h.to_recall_dict() for h in hits]}
