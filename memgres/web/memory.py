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


def every_space(conn, q: str = "") -> list:
    """Every namespace on the deployment, for a superadmin choosing one to open:
    name, owner, how many records and members. Metadata only."""
    q = (q or "").strip()[:200]
    where, params = "", {}
    if q:
        where = "WHERE n.name ILIKE %(q)s OR u.name ILIKE %(q)s OR u.full_name ILIKE %(q)s"
        params["q"] = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.id::text, n.name, n.description, u.id::text, "
            "       COALESCE(NULLIF(u.full_name, ''), NULLIF(u.name, ''), u.email), "
            "       (SELECT count(*) FROM namespace_member m WHERE m.namespace_id = n.id) "
            f"FROM namespace n JOIN app_user u ON u.id = n.owner_user_id {where} "
            "ORDER BY lower(n.name), n.id LIMIT 1000", params)
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        counts = {}
        if ids:
            w, prm = build_filters(ids, None, None)
            cur.execute(f"SELECT namespace, count(*) FROM memory WHERE {w} GROUP BY namespace", prm)
            counts = {str(ns): n for ns, n in cur.fetchall()}
    return [{"id": i, "name": n, "description": d, "owner": {"id": oid, "name": oname},
             "members": mc + 1, "records": counts.get(i, 0)} for i, n, d, oid, oname, mc in rows]


def one_space(conn, principal, space_id: str) -> dict:
    """What the sidebar needs to show a space the caller opened by id — for a
    superadmin, one it reaches by its role rather than as a member."""
    with conn.transaction():
        nsid, _perm = identity.resolve_space(conn, principal, space_id=space_id)
    membership = identity.reaches(conn, principal.user_id, nsid) if principal.user_id else None
    where, params = build_filters([nsid], None, None)
    with conn.cursor() as cur:
        cur.execute("SELECT name, description, owner_user_id::text FROM namespace WHERE id = %s", (nsid,))
        name, desc, owner = cur.fetchone()
        cur.execute(f"SELECT count(*) FROM memory WHERE {where}", params)
        records = cur.fetchone()[0]
    return {"id": nsid, "name": name, "description": desc, "records": records,
            # not a member: the role reaches it, and a superadmin administers any space
            "permission": membership or "admin", "member": membership is not None,
            "mine": owner == principal.user_id, "alias": None, "waiting": 0}


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

    @app.get("/ui/api/spaces/{space_id}")
    def space_meta(space_id: str, request: Request):
        """One space by id. A member gets what the sidebar shows; a superadmin
        gets any space — its role reads every one; anyone else: not found."""
        _, p = _reader(request)
        with pool.connection() as conn:
            return _guard(lambda: one_space(conn, p, identity._as_uuid(space_id)))

    @app.get("/ui/api/admin/spaces")
    def all_spaces(request: Request, q: str = ""):
        s = panel["session"](request)
        if s.role != "superadmin":
            # a user manager administers accounts, not what is inside spaces
            raise HTTPException(403, "superadmins only")
        with pool.connection() as conn:
            return {"spaces": every_space(conn, q)}

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
