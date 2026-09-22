"""People: a profile, write activity, and the administrators' directory.

Who sees what:

* **yourself** — everything: spaces, sign-in methods, tokens, activity;
* **a service administrator** (user_manager, superadmin) — the same about
  anyone, plus the switches: role, disabled, tokens, sessions, sign-in methods.
  The control plane's own rules still apply: a user_manager cannot act on an
  administrator's account, and only a superadmin hands out roles;
* **someone you share a space with** — who they are (name, department,
  position) and what they wrote in the spaces you can both read;
* **what someone wrote** — the activity chart and the list of recent edits —
  is only ever shown where the VIEWER can read too: a superadmin reads every
  space, everyone else (a user_manager included) only the spaces they are in.
  so the PAGE never displays records from a space the viewer cannot open.
  That is a rule about this page, not a containment guarantee about the role:
  an administrator who can mint a token FOR someone can read what that someone
  reads, which docs/TENANCY.md states outright;
* **anyone else** — not found. There is no directory for ordinary users.

Activity is writes only, taken from memory history: what someone created,
changed, moved or retagged. Reads are not tracked per person. History goes away
with a record that is erased, so a person's past activity can shrink.
"""

import datetime as dt
from typing import Optional

from .. import admin, identity
from ..identity import ADMIN_ROLES

ACTIVITY_DAYS = 182          # 26 weeks, the width of the chart
MAX_PEOPLE_PAGE = 100


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _uuid(value) -> str:
    try:
        return identity._as_uuid(value)
    except ValueError:
        raise Refused(404, "not found") from None


def _shared_spaces(cur, viewer: str, target: str) -> list:
    cur.execute(
        "WITH reach AS ("
        "  SELECT id AS ns, owner_user_id AS uid FROM namespace "
        "  UNION SELECT namespace_id, user_id FROM namespace_member) "
        "SELECT a.ns FROM reach a JOIN reach b ON a.ns = b.ns "
        "WHERE a.uid = %s AND b.uid = %s", (viewer, target))
    return [str(r[0]) for r in cur.fetchall()]


def _reachable(conn, user_id: str) -> list:
    return [s["id"] for s in identity.list_spaces(conn, user_id)]


def activity(conn, user_id: str, *, spaces: Optional[list]) -> dict:
    """Writes by day over the chart's window, with totals by space and by kind.
    ``spaces=None`` counts every space (an administrator's view); otherwise only
    the spaces listed."""
    params = [user_id, ACTIVITY_DAYS]
    where = ""
    if spaces is not None:
        if not spaces:
            return {"days": [], "by_space": [], "by_op": {}, "total": 0, "window_days": ACTIVITY_DAYS}
        where = " AND m.namespace = ANY(%s::text[])"
        params.append(spaces)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (h.created_at AT TIME ZONE 'UTC')::date, m.namespace, h.op, count(*) "
            "FROM memory_history h JOIN memory m ON m.id = h.memory_id "
            "WHERE h.author_user_id = %s AND h.created_at > now() - make_interval(days => %s)"
            + where + " GROUP BY 1, 2, 3", params)
        rows = cur.fetchall()
        ns_ids = sorted({r[1] for r in rows if r[1]})
        names = {}
        if ns_ids:
            cur.execute("SELECT id::text, name FROM namespace WHERE id = ANY(%s::uuid[])", (ns_ids,))
            names = dict(cur.fetchall())
    days, by_space, by_op, total = {}, {}, {}, 0
    for day, ns, op, n in rows:
        days[day] = days.get(day, 0) + n
        by_space[ns] = by_space.get(ns, 0) + n
        by_op[op] = by_op.get(op, 0) + n
        total += n
    return {
        "days": [{"day": d.isoformat(), "n": n} for d, n in sorted(days.items())],
        "by_space": sorted(({"id": ns, "name": names.get(ns, ""), "n": n} for ns, n in by_space.items()),
                           key=lambda x: -x["n"]),
        "by_op": by_op,
        "total": total,
        "window_days": ACTIVITY_DAYS,
        "today": dt.datetime.now(dt.timezone.utc).date().isoformat(),
    }


RECENT_EDITS = 30


def recent_edits(conn, user_id: str, *, spaces: Optional[list]) -> list:
    """The person's latest changes, newest first — record, space, kind, when.
    ``spaces=None`` means every space (a superadmin looking)."""
    params: list = [user_id]
    where = ""
    if spaces is not None:
        if not spaces:
            return []
        where = " AND m.namespace = ANY(%s::text[])"
        params.append(spaces)
    params.append(RECENT_EDITS)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT h.created_at, h.op, m.id::text, m.namespace, m.path::text, m.title, n.name "
            "FROM memory_history h JOIN memory m ON m.id = h.memory_id "
            "JOIN namespace n ON n.id::text = m.namespace "
            "WHERE h.author_user_id = %s" + where + " ORDER BY h.created_at DESC, h.id DESC LIMIT %s", params)
        return [{"at": at, "op": op, "record_id": rid, "space_id": ns, "path": path, "title": title or "",
                 "space": sname} for at, op, rid, ns, path, title, sname in cur.fetchall()]


def _viewer_scope(conn, viewer) -> Optional[list]:
    """The spaces whose contents this viewer may see: every one for a superadmin
    (its role reads them all), otherwise its own."""
    if viewer.user_id is None or viewer.role == "superadmin":
        return None
    return _reachable(conn, viewer.user_id)


def _account(cur, user_id: str):
    cur.execute("SELECT id, name, full_name, email, department, position, role, "
                "disabled_at IS NOT NULL, created_at, can_create_namespace FROM app_user WHERE id = %s",
                (user_id,))
    return cur.fetchone()


def profile(conn, viewer, target_id: str, *, providers: dict) -> dict:
    """``viewer`` is the session. Raises Refused(404) for anyone the viewer may
    not look at — the same answer as an id that does not exist."""
    from . import admission, tokens
    target_id = _uuid(target_id)
    is_self = viewer.user_id == target_id
    is_service_admin = viewer.role in ADMIN_ROLES
    with conn.cursor() as cur:
        row = _account(cur, target_id)
        if row is None:
            raise Refused(404, "not found")
        shared = [] if (is_self or is_service_admin or viewer.user_id is None) else \
            _shared_spaces(cur, viewer.user_id, target_id)
    if not (is_self or is_service_admin or shared):
        raise Refused(404, "not found")
    uid, name, full_name, email, dept, position, role, disabled, created, can_create = row
    person = {"id": str(uid), "name": name, "full_name": full_name, "department": dept,
              "position": position}
    if not (is_self or is_service_admin):
        # a colleague: who they are, and only where your spaces meet
        mine = _reachable(conn, viewer.user_id)
        together = [s for s in shared if s in mine]
        with conn.cursor() as cur:
            cur.execute("SELECT id::text, name FROM namespace WHERE id = ANY(%s::uuid[]) ORDER BY name",
                        (together,))
            spaces = [{"id": i, "name": n} for i, n in cur.fetchall()]
        return {"view": "colleague", "person": person, "shared_spaces": spaces,
                "activity": activity(conn, str(uid), spaces=together),
                "recent": recent_edits(conn, str(uid), spaces=together)}

    # who holds authority on the server is not a colleague's business
    person.update({"role": role, "email": email, "disabled": bool(disabled), "created_at": created,
                   "can_create_namespace": bool(can_create)})
    spaces = identity.list_spaces(conn, str(uid))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM web_session WHERE user_id = %s AND revoked_at IS NULL "
                    "AND expires_at > now()", (str(uid),))
        live_sessions = cur.fetchone()[0]
    # A user_manager hands out access without gaining it, so an administrator's
    # tokens and sign-in methods are not theirs to see (admin.list_tokens
    # refuses the same thing).
    may_manage = is_self or viewer.role == "superadmin" or role not in ADMIN_ROLES
    scope = _viewer_scope(conn, viewer)
    out = {
        "view": "self" if is_self else "admin",
        "person": person,
        "spaces": [{"id": s["id"], "name": s["name"], "permission": s["permission"], "mine": s["mine"]}
                   for s in spaces],
        "signins": admission.identities(conn, str(uid), providers) if may_manage else None,
        "tokens": tokens.list_own(conn, str(uid)) if may_manage else None,
        "sessions": live_sessions,
        "activity": activity(conn, str(uid), spaces=scope),
        "recent": recent_edits(conn, str(uid), spaces=scope),
    }
    if not is_self:
        # what this administrator may do to this account, by the rules that
        # will enforce it
        out["can"] = {"manage": may_manage, "set_role": viewer.role == "superadmin"}
    return out


# ─── the directory (service administrators) ──────────────────────────────────
def directory(conn, *, q: str = "", limit: int = 50, offset: int = 0) -> dict:
    q = (q or "").strip()[:200]
    limit = max(1, min(int(limit), MAX_PEOPLE_PAGE))
    where, params = "", []
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = ("WHERE u.name ILIKE %(q)s OR u.full_name ILIKE %(q)s OR u.email ILIKE %(q)s "
                 "OR u.department ILIKE %(q)s OR EXISTS (SELECT 1 FROM app_user_identity i "
                 "WHERE i.user_id = u.id AND i.email ILIKE %(q)s)")
        params = {"q": like}
    else:
        params = {}
    params.update({"limit": limit, "offset": max(0, int(offset))})
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM app_user u {where}", params)
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT u.id, u.name, u.full_name, u.email, u.department, u.role, u.disabled_at IS NOT NULL, "
            "       u.created_at, "
            "       (SELECT max(last_login_at) FROM app_user_identity i WHERE i.user_id = u.id), "
            "       (SELECT max(created_at) FROM memory_history h WHERE h.author_user_id = u.id), "
            "       (SELECT count(*) FROM app_user_identity i WHERE i.user_id = u.id) "
            f"FROM app_user u {where} "
            "ORDER BY u.disabled_at IS NOT NULL, lower(COALESCE(NULLIF(u.full_name, ''), u.name)), u.id "
            "LIMIT %(limit)s OFFSET %(offset)s", params)
        people = [{"id": str(i), "name": n, "full_name": fn, "email": e, "department": d, "role": r,
                   "disabled": bool(off), "created_at": c, "last_signin_at": ls, "last_write_at": lw,
                   "signins": si}
                  for i, n, fn, e, d, r, off, c, ls, lw, si in cur.fetchall()]
    return {"people": people, "total": total, "limit": limit, "offset": offset}


def _guard(fn):
    try:
        return fn()
    except admin.Forbidden as e:
        raise Refused(403, str(e)) from None
    except admin.Lockout as e:
        raise Refused(409, str(e)) from None
    except identity.SpaceNotFound:
        raise Refused(404, "not found") from None
    except identity.AuthError as e:
        raise Refused(409, str(e)) from None
    except ValueError as e:
        raise Refused(422, str(e)) from None


def end_sessions(conn, p, user_id: str) -> int:
    user_id = _uuid(user_id)
    _guard(lambda: admin._require_target_is_plain_user(conn, admin.require_manage_users(p), user_id,
                                                       "ending sessions"))
    with conn.cursor() as cur:
        cur.execute("UPDATE web_session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                    (user_id,))
        return cur.rowcount


def unlink_signin(conn, p, user_id: str, identity_id: str) -> None:
    """An administrator removes a sign-in method — the last one too: that is how
    a wrongly linked sign-in is taken back. Its sessions end with it."""
    user_id, identity_id = _uuid(user_id), _uuid(identity_id)
    _guard(lambda: admin._require_target_is_plain_user(conn, admin.require_manage_users(p), user_id,
                                                       "removing a sign-in method"))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app_user_identity WHERE id = %s AND user_id = %s RETURNING id",
                    (identity_id, user_id))
        if cur.fetchone() is None:
            raise Refused(404, "not found")
        cur.execute("UPDATE web_session SET revoked_at = now() WHERE user_id = %s AND identity_id = %s "
                    "AND revoked_at IS NULL", (user_id, identity_id))


def revoke_token(conn, p, user_id: str, token_id: str) -> None:
    user_id, token_id = _uuid(user_id), _uuid(token_id)
    if identity.token_owner(conn, token_id) != user_id:
        raise Refused(404, "not found")
    _guard(lambda: admin.revoke_token(conn, p, token_id=token_id))


PROFILE_EDITABLE = ("full_name", "email", "department", "position")


def edit(conn, p, user_id: str, changes: dict) -> None:
    user_id = _uuid(user_id)
    fields = {}
    for k, v in changes.items():
        if k not in PROFILE_EDITABLE or v is None:
            continue
        if not isinstance(v, str):
            raise Refused(422, f"{k} must be text")
        fields[k] = v.strip()
    if "email" in fields:
        if fields["email"]:
            from .spaces import Refused as SpaceRefused, normalize_email
            try:
                fields["email"] = normalize_email(fields["email"])
            except SpaceRefused as e:
                raise Refused(422, str(e)) from None
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app_user WHERE lower(email) = lower(%s) AND id <> %s",
                            (fields["email"], user_id))
                if cur.fetchone():
                    raise Refused(409, "another account already has this email")
        else:
            # an empty email is "none", which the unique index allows many of
            with conn.cursor() as cur:
                _guard(lambda: admin._require_target_is_plain_user(conn, admin.require_manage_users(p), user_id,
                                                                   "editing a profile"))
                cur.execute("UPDATE app_user SET email = NULL WHERE id = %s", (user_id,))
            fields.pop("email")
    for k, v in fields.items():
        if len(v) > 200:
            raise Refused(422, f"{k} is longer than 200 characters")
    _guard(lambda: admin.edit_user(conn, p, user_id=user_id, **fields))


def create_person(conn, p, fields: dict) -> str:
    """An account made by an administrator — for someone who will sign in later
    (the email is how their first sign-in finds it) or for a service that only
    ever uses tokens. It gets no spaces and no rights: those are separate acts."""
    clean = {}
    for k in ("name", "full_name", "email", "department", "position"):
        v = fields.get(k)
        if v is None:
            continue
        if not isinstance(v, str):
            raise Refused(422, f"{k} must be text")
        v = v.strip()
        if len(v) > 200:
            raise Refused(422, f"{k} is longer than 200 characters")
        if v:
            clean[k] = v
    if "email" in clean:
        from .spaces import Refused as SpaceRefused, normalize_email
        try:
            clean["email"] = normalize_email(clean["email"])
        except SpaceRefused as e:
            raise Refused(422, str(e)) from None
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM app_user WHERE lower(email) = lower(%s)", (clean["email"],))
            if cur.fetchone():
                raise Refused(409, "another account already has this email")
    if not (clean.get("full_name") or clean.get("email") or clean.get("name")):
        raise Refused(422, "give the account a name or an email")
    name = clean.pop("name", None) or clean.get("email") or clean.get("full_name")
    return _guard(lambda: admin.create_user(conn, p, name=name, **clean))


def set_can_create_spaces(conn, p, user_id: str, allowed: bool) -> None:
    user_id = _uuid(user_id)
    _guard(lambda: admin.set_can_create_namespace(conn, p, user_id=user_id, allowed=bool(allowed)))


def issue_for(conn, p, user_id: str, *, label: str, permission: str,
              namespace_id: Optional[str], expires_days: int) -> dict:
    """A token for someone else — the same shape a person gets for themselves
    (read or write, always expiring, optionally one of their spaces), minted by
    an administrator for an account that cannot do it itself: a service, or
    someone who has not signed in yet. The secret is shown once, to the
    administrator, who hands it over — the same exemption from
    ``MEMGRES_TOKEN_SINK`` the self-service door takes, and for the same reason:
    the sink keeps secrets out of *agent* transcripts, and the reader here is a
    human looking at a dialog that says "copy it now".

    The cap on live tokens is the account's, not the issuer's: an administrator
    minting for someone cannot fill the table past what that person could fill
    it to themselves."""
    from .tokens import EXPIRY_CHOICES, MAX_LABEL, MAX_LIVE_TOKENS, PERMISSIONS
    user_id = _uuid(user_id)
    label = (label or "").strip()
    if len(label) > MAX_LABEL:
        raise Refused(422, f"the label is longer than {MAX_LABEL} characters")
    if permission not in PERMISSIONS:
        raise Refused(422, "access must be read or write")
    if expires_days not in EXPIRY_CHOICES:
        raise Refused(422, f"expiry must be one of {', '.join(map(str, EXPIRY_CHOICES))} days")
    if namespace_id:
        namespace_id = _uuid(namespace_id)
        if identity.reaches(conn, user_id, namespace_id) is None:
            raise Refused(422, "that person cannot open this space — add them to it first")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM app_user WHERE id = %s", (user_id,))
        if cur.fetchone() is None:
            raise Refused(404, "not found")
        cur.execute("SELECT count(*) FROM token WHERE user_id = %s AND revoked_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at > now())", (user_id,))
        if cur.fetchone()[0] >= MAX_LIVE_TOKENS:
            raise Refused(409, f"that account already has {MAX_LIVE_TOKENS} active tokens — "
                               "revoke some it no longer uses")
    out = _guard(lambda: admin.issue_token(conn, p, user_id=user_id, namespace_id=namespace_id or None,
                                           permission=permission, label=label, expires_days=expires_days,
                                           defer_delivery=True))
    return {"id": out["id"], "token": out["secret"], "permission": permission,
            "namespace_id": namespace_id or None, "expires_days": expires_days}


# ─── record history (for authors) ────────────────────────────────────────────
HISTORY_PAGE = 25


def record_history(store, principal, space_id: str, record_id: str, *,
                   before_seq: Optional[int] = None) -> dict:
    """Who changed a record and when — the way into a person's profile from
    what they wrote. No diffs: the panel shows the record as it is now.

    A page at a time, newest first. A memory that has been edited for a year
    otherwise answers with its entire chain, which is a slow request and a list
    nobody scrolls."""
    rows = store.history(principal, id=record_id, space_id=space_id,
                         limit=HISTORY_PAGE + 1, before_seq=before_seq)
    more = len(rows) > HISTORY_PAGE
    rows = rows[-HISTORY_PAGE:] if more else rows        # rows are oldest-first
    return {"history": [{"seq": r["seq"], "op": r["op"], "at": r["created_at"],
                         "author_id": r["author_user_id"], "author": r["author_name"],
                         "reason": r["reason"], "path_before": r["path_before"],
                         "path_after": r["path_after"]}
                        for r in reversed(rows)],
            "more": more}


def record_blame(store, principal, space_id: str, record_id: str) -> dict:
    """The body split into runs, each carrying who last touched it and when.

    Grouped rather than per-line: a long memory edited by two people is a
    handful of blocks, and a list of five hundred identically-attributed lines
    is not something a person reads. The text comes back as written — the panel
    shows it verbatim here, because blame is about lines, and rendering Markdown
    across block boundaries would put the attribution in the wrong places.
    """
    blocks = store.annotate_grouped(principal, id=record_id, space_id=space_id)
    return {"blame": [{"start": b["start"], "end": b["end"], "seq": b.get("seq"),
                       "op": b.get("op"), "at": b.get("created_at"),
                       "author_id": b.get("author_user_id"),
                       "author": b.get("author_name"),
                       "reason": b.get("reason"), "text": b.get("text", "")}
                      for b in blocks]}


# ─── HTTP ────────────────────────────────────────────────────────────────────
def mount(app, cfg, pool, panel, providers, make_store) -> None:
    from fastapi import Body, HTTPException, Query, Request

    from .sessions import control_principal

    def _run(fn, *, tx=True):
        with pool.connection() as conn:
            try:
                if tx:
                    with conn.transaction():
                        return fn(conn)
                return fn(conn)
            except Refused as e:
                raise HTTPException(e.status, str(e))

    def _admin(request: Request, changing: bool = False):
        s = panel["changing"](request) if changing else panel["session"](request)
        if s.role not in ADMIN_ROLES:
            raise HTTPException(403, "administrators only")
        return s

    @app.get("/ui/api/people/{user_id}")
    def person(user_id: str, request: Request):
        s = panel["session"](request)
        return _run(lambda conn: profile(conn, s, user_id, providers=providers), tx=False)

    @app.get("/ui/api/admin/people")
    def people(request: Request, q: str = "", limit: int = 50, offset: int = 0):
        _admin(request)
        return _run(lambda conn: directory(conn, q=q, limit=limit, offset=offset), tx=False)

    @app.patch("/ui/api/admin/people/{user_id}")
    def edit_person(user_id: str, request: Request, changes: dict = Body(...)):
        p = control_principal(_admin(request, changing=True))
        _run(lambda conn: edit(conn, p, user_id, changes))
        return {"user_id": user_id}

    @app.post("/ui/api/admin/people", status_code=201)
    def new_person(request: Request, fields: dict = Body(...)):
        p = control_principal(_admin(request, changing=True))
        return {"id": _run(lambda conn: create_person(conn, p, fields))}

    @app.post("/ui/api/admin/people/{user_id}/can-create-spaces")
    def can_create(user_id: str, request: Request, allowed: bool = Body(..., embed=True)):
        p = control_principal(_admin(request, changing=True))
        _run(lambda conn: set_can_create_spaces(conn, p, user_id, allowed))
        return {"user_id": user_id, "can_create_namespace": bool(allowed)}

    @app.post("/ui/api/admin/people/{user_id}/tokens", status_code=201)
    def token_for(user_id: str, request: Request, label: str = Body("", embed=True),
                  permission: str = Body("write", embed=True),
                  namespace_id: Optional[str] = Body(None, embed=True),
                  expires_days: int = Body(90, embed=True)):
        p = control_principal(_admin(request, changing=True))
        return _run(lambda conn: issue_for(conn, p, user_id, label=label, permission=permission,
                                           namespace_id=namespace_id, expires_days=expires_days))

    @app.post("/ui/api/admin/people/{user_id}/role")
    def set_role(user_id: str, request: Request, role: str = Body(..., embed=True)):
        p = control_principal(_admin(request, changing=True))
        return _run(lambda conn: _guard(lambda: admin.set_role(conn, p, user_id=_uuid(user_id), role=role)))

    @app.post("/ui/api/admin/people/{user_id}/disabled")
    def set_disabled(user_id: str, request: Request, disabled: bool = Body(..., embed=True)):
        s = _admin(request, changing=True)
        try:
            same = s.user_id is not None and identity._as_uuid(user_id) == s.user_id
        except ValueError:
            raise HTTPException(404, "not found")
        if same and disabled:
            raise HTTPException(409, "you can’t switch off your own account")
        p = control_principal(s)
        return _run(lambda conn: _guard(lambda: admin.set_disabled(conn, p, user_id=_uuid(user_id),
                                                                   disabled=disabled)))

    @app.post("/ui/api/admin/people/{user_id}/sessions/end")
    def sessions_end(user_id: str, request: Request):
        p = control_principal(_admin(request, changing=True))
        return {"ended": _run(lambda conn: end_sessions(conn, p, user_id))}

    @app.delete("/ui/api/admin/people/{user_id}/signins/{identity_id}")
    def signin_remove(user_id: str, identity_id: str, request: Request):
        p = control_principal(_admin(request, changing=True))
        _run(lambda conn: unlink_signin(conn, p, user_id, identity_id))
        return {"unlinked": identity_id}

    @app.delete("/ui/api/admin/people/{user_id}/tokens/{token_id}")
    def token_revoke(user_id: str, token_id: str, request: Request):
        p = control_principal(_admin(request, changing=True))
        _run(lambda conn: revoke_token(conn, p, user_id, token_id))
        return {"revoked": token_id}

    @app.get("/ui/api/spaces/{space_id}/records/{record_id}/blame")
    def blame(space_id: str, record_id: str, request: Request):
        from ..identity import AuthError, SpaceNotFound
        from ..store import NotFound
        s = panel["session"](request)
        if s.user_id is None:
            raise HTTPException(409, "the administrator token has no memory to show")
        with pool.connection() as conn:
            try:
                sid, rid = identity._as_uuid(space_id), identity._as_uuid(record_id)
                return record_blame(make_store(conn), panel["principal"](s), sid, rid)
            except (NotFound, SpaceNotFound, AuthError, KeyError, ValueError):
                raise HTTPException(404, "not found")

    @app.get("/ui/api/spaces/{space_id}/records/{record_id}/history")
    def history(space_id: str, record_id: str, request: Request,
                before_seq: Optional[int] = Query(None, ge=0, le=2 ** 31 - 1)):
        from ..identity import AuthError, SpaceNotFound
        from ..store import NotFound
        s = panel["session"](request)
        if s.user_id is None:
            raise HTTPException(409, "the administrator token has no memory to show")
        with pool.connection() as conn:
            try:
                sid, rid = identity._as_uuid(space_id), identity._as_uuid(record_id)
                return record_history(make_store(conn), panel["principal"](s), sid, rid,
                                      before_seq=before_seq)
            except (NotFound, SpaceNotFound, AuthError, KeyError, ValueError):
                raise HTTPException(404, "not found")
