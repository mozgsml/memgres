"""Who is in a space: members, invitations by email, requests to join.

Authorization is the control plane's own (:mod:`memgres.admin`), called with the
session's control principal — the same checks the REST API and MCP apply, not a
second copy of them:

* members, invitations and requests of a space — its administrators (the owner,
  an ``admin`` member, a superadmin);
* handing the space to someone else — the owner or a superadmin;
* asking to join, or leaving — the person themselves;
* making a space of your own — anyone the deployment granted that right
  (``can_create_namespace``, a switch on the person's page).

**An invitation opens a space, never the server.** Inviting an address that
already has an account adds that account at once. Otherwise the invitation waits
for a sign-in whose provider vouches for the address (``email_verified``), and is
applied then. Whether that sign-in gets in at all is still up to the provider's
rules and the administrators: an invitation does not skip the queue.

The answer to an invitation is the same whether or not the address has an
account — the same status, the same cap. The member list the inviter sees
afterwards does show who was added, and that cannot be hidden from the person
who has to manage the list: a space administrator can learn that an address has
an account here. Two things keep that from becoming a directory: additions are
rate-limited per person, and a colleague's profile says who they are, not what
authority they hold. Nothing is ever lowered by adding someone again.
"""

import re
from typing import Optional

from .. import admin, identity

INVITE_DAYS = 30
MAX_OPEN_INVITES = 200          # per space
ADDS_PER_HOUR = 60              # people one person may add or invite, across spaces
PERMISSIONS = ("read", "write", "admin")
_RANK = {"read": 1, "write": 2, "admin": 3}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _uuid(value, what="not found") -> str:
    try:
        return identity._as_uuid(value)
    except ValueError:
        raise Refused(404, what) from None


def _perm(value: str) -> str:
    if value not in PERMISSIONS:
        raise Refused(422, "permission must be read, write or admin")
    return value


def _require_admin(conn, p, space_id: str) -> None:
    """Administrators of the space only. Not being one reads as the space not
    existing — the same answer the memory routes give."""
    try:
        admin.require_namespace_admin(conn, p, space_id)
    except (admin.Forbidden, identity.SpaceNotFound, identity.AuthError):
        raise Refused(404, "not found") from None


def normalize_email(value: str) -> str:
    email = (value or "").strip()
    if len(email) > 254 or not _EMAIL.match(email):
        raise Refused(422, "that doesn’t look like an email address")
    return email


def _grant_at_least(cur, space_id: str, user_id: str, permission: str) -> None:
    """Make someone a member with at least this permission. Stronger access
    they already have stays — changing a member's permission is its own act."""
    cur.execute(
        "INSERT INTO namespace_member (namespace_id, user_id, permission) VALUES (%s, %s, %s) "
        "ON CONFLICT (namespace_id, user_id) DO UPDATE SET permission = CASE "
        "WHEN array_position(ARRAY['read','write','admin'], EXCLUDED.permission) > "
        "     array_position(ARRAY['read','write','admin'], namespace_member.permission) "
        "THEN EXCLUDED.permission ELSE namespace_member.permission END",
        (space_id, user_id, permission))


def _person(row) -> dict:
    uid, name, full_name, email, disabled = row
    return {"id": str(uid), "name": name, "full_name": full_name, "email": email,
            "disabled": bool(disabled)}


# ─── reading ─────────────────────────────────────────────────────────────────
def overview(conn, p, space_id: str) -> dict:
    """Everything a space's administrator manages, in one answer."""
    space_id = _uuid(space_id)
    _require_admin(conn, p, space_id)
    with conn.cursor() as cur:
        cur.execute("SELECT name, owner_user_id, now() FROM namespace WHERE id = %s", (space_id,))
        name, owner, now = cur.fetchone()
        cur.execute(
            "SELECT u.id, u.name, u.full_name, u.email, u.disabled_at IS NOT NULL, 'admin', TRUE, n.created_at "
            "FROM namespace n JOIN app_user u ON u.id = n.owner_user_id "
            "WHERE n.id = %(s)s "
            "UNION ALL "
            "SELECT u.id, u.name, u.full_name, u.email, u.disabled_at IS NOT NULL, m.permission, FALSE, m.created_at "
            "FROM namespace_member m JOIN app_user u ON u.id = m.user_id "
            "WHERE m.namespace_id = %(s)s",
            {"s": space_id})
        members = []
        for row in cur.fetchall():
            d = _person(row[:5])
            d.update({"permission": row[5], "owner": bool(row[6]), "since": row[7]})
            members.append(d)
        members.sort(key=lambda d: (not d["owner"], -_RANK[d["permission"]],
                                    (d["full_name"] or d["name"] or d["email"] or "").lower()))
        cur.execute(
            "SELECT i.id, i.email, i.permission, i.created_at, i.expires_at, "
            "       COALESCE(NULLIF(u.full_name, ''), NULLIF(u.name, ''), u.email), i.invited_by "
            "FROM space_invite i LEFT JOIN app_user u ON u.id = i.invited_by "
            "WHERE i.namespace_id = %s AND i.accepted_at IS NULL ORDER BY i.created_at DESC",
            (space_id,))
        invites = [{"id": str(i), "email": e, "permission": perm, "created_at": c,
                    "expires_at": x, "invited_by": by, "expired": x <= now,
                    "_by": str(by_id) if by_id else None}
                   for i, e, perm, c, x, by, by_id in cur.fetchall()]
        for inv in invites:
            # an invitation carries its sender's authority; without it, it will not apply
            inv["lapsed"] = not _still_may_grant(cur, inv.pop("_by"), space_id)
        cur.execute(
            "SELECT r.id, r.requested_permission, r.created_at, "
            "       u.id, u.name, u.full_name, u.email, u.disabled_at IS NOT NULL "
            "FROM access_request r JOIN app_user u ON u.id = r.requester_user_id "
            "JOIN namespace n ON n.id = r.namespace_id "
            "WHERE r.namespace_id = %s AND r.status = 'pending' "
            # someone who got in another way meanwhile is not waiting any more
            "AND r.requester_user_id <> n.owner_user_id "
            "AND NOT EXISTS (SELECT 1 FROM namespace_member m WHERE m.namespace_id = r.namespace_id "
            "                AND m.user_id = r.requester_user_id) "
            "ORDER BY r.created_at",
            (space_id,))
        requests = [{"id": str(rid), "permission": perm, "created_at": c, "person": _person(rest)}
                    for rid, perm, c, *rest in cur.fetchall()]
    may_transfer = True
    try:
        admin.require_namespace_owner(conn, p, space_id)
    except (admin.Forbidden, identity.SpaceNotFound):
        may_transfer = False
    return {"space": {"id": space_id, "name": name, "owner_user_id": str(owner)},
            "members": members, "invites": invites, "requests": requests,
            "can_transfer": may_transfer, "invite_days": INVITE_DAYS}


# ─── members ─────────────────────────────────────────────────────────────────
def _accounts_with_email(cur, email: str) -> list:
    cur.execute(
        "SELECT id FROM app_user WHERE email IS NOT NULL AND lower(email) = lower(%(e)s) "
        "UNION SELECT user_id FROM app_user_identity WHERE email IS NOT NULL AND lower(email) = lower(%(e)s)",
        {"e": email})
    return [str(r[0]) for r in cur.fetchall()]


def invite(conn, p, space_id: str, *, email: str, permission: str, inviter_id: Optional[str]) -> dict:
    """Add the account with this email, or leave an invitation for it.

    Two accounts claiming one address (a profile email on one, a provider's on
    another) is not a choice this form makes: it becomes an invitation, applied
    to whoever next signs in with that address verified."""
    space_id = _uuid(space_id)
    _require_admin(conn, p, space_id)
    if inviter_id is None:
        # an invitation carries its sender's authority, and the env token has no
        # account to carry it — it would be stored and never apply
        raise Refused(409, "the administrator token has no account to invite from — sign in as a person")
    email, permission = normalize_email(email), _perm(permission)
    with conn.cursor() as cur:
        # the cap is checked before anything depends on the address, so a full
        # space refuses every address alike
        cur.execute("SELECT count(*) FROM space_invite WHERE namespace_id = %s AND accepted_at IS NULL "
                    "AND lower(email) <> lower(%s)", (space_id, email))
        if cur.fetchone()[0] >= MAX_OPEN_INVITES:
            raise Refused(409, f"this space already has {MAX_OPEN_INVITES} open invitations — "
                               "cancel some that nobody used")
        matches = _accounts_with_email(cur, email)
        if len(matches) == 1:
            uid = matches[0]
            if identity.namespace_owner(conn, space_id) != uid:
                _grant_at_least(cur, space_id, uid, permission)
            # an older invitation for the same address has been answered by this
            cur.execute("DELETE FROM space_invite WHERE namespace_id = %s AND lower(email) = lower(%s) "
                        "AND accepted_at IS NULL", (space_id, email))
            return {"status": "invited"}
        cur.execute(
            "INSERT INTO space_invite (namespace_id, email, permission, invited_by, expires_at) "
            "VALUES (%s, %s, %s, %s, now() + make_interval(days => %s)) "
            "ON CONFLICT (namespace_id, lower(email)) WHERE accepted_at IS NULL DO UPDATE SET "
            "permission = EXCLUDED.permission, invited_by = EXCLUDED.invited_by, "
            "created_at = now(), expires_at = EXCLUDED.expires_at",
            (space_id, email, permission, inviter_id, INVITE_DAYS))
    return {"status": "invited"}


def cancel_invite(conn, p, space_id: str, invite_id: str) -> None:
    space_id, invite_id = _uuid(space_id), _uuid(invite_id)
    _require_admin(conn, p, space_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM space_invite WHERE id = %s AND namespace_id = %s AND accepted_at IS NULL "
                    "RETURNING id", (invite_id, space_id))
        if cur.fetchone() is None:
            raise Refused(404, "not found")


def set_permission(conn, p, space_id: str, user_id: str, permission: str) -> None:
    space_id, user_id = _uuid(space_id), _uuid(user_id)
    _require_admin(conn, p, space_id)
    permission = _perm(permission)
    if identity.namespace_owner(conn, space_id) == user_id:
        raise Refused(409, "the owner always has full access — hand the space over to change that")
    with conn.cursor() as cur:
        cur.execute("UPDATE namespace_member SET permission = %s WHERE namespace_id = %s AND user_id = %s "
                    "RETURNING user_id", (permission, space_id, user_id))
        if cur.fetchone() is None:
            raise Refused(404, "not a member")


def remove(conn, p, space_id: str, user_id: str, *, actor_id: Optional[str]) -> None:
    """Take someone out of the space — or leave it yourself, which needs no
    authority beyond being in it."""
    space_id, user_id = _uuid(space_id), _uuid(user_id)
    if actor_id is None or actor_id != user_id:
        _require_admin(conn, p, space_id)
    elif identity.reaches(conn, user_id, space_id) is None:
        raise Refused(404, "not found")
    try:
        removed = identity.remove_member(conn, space_id, user_id)
    except identity.AuthError:
        raise Refused(409, "the owner can’t leave or be removed — hand the space over first") from None
    except identity.SpaceNotFound:
        raise Refused(404, "not found") from None
    if not removed:
        raise Refused(404, "not a member")


def transfer(conn, p, space_id: str, user_id: str, *, keep_me: bool) -> dict:
    """Hand the space to one of its members. The panel only offers members:
    giving a space to someone who has never seen it is an administrator's act,
    through the API."""
    space_id, user_id = _uuid(space_id), _uuid(user_id)
    try:
        admin.require_namespace_owner(conn, p, space_id)
    except (admin.Forbidden, identity.SpaceNotFound):
        raise Refused(403, "only the owner or a superadmin can hand this space over") from None
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM namespace_member WHERE namespace_id = %s AND user_id = %s",
                    (space_id, user_id))
        if cur.fetchone() is None:
            raise Refused(422, "the new owner has to be a member of the space already")
        cur.execute("SELECT disabled_at IS NOT NULL FROM app_user WHERE id = %s", (user_id,))
        if cur.fetchone()[0]:
            raise Refused(409, "that account is switched off")
    try:
        return identity.transfer_namespace(conn, space_id, user_id,
                                           keep_previous_owner="admin" if keep_me else None)
    except identity.SpaceAmbiguous as e:
        raise Refused(409, str(e)) from None


def create_own(conn, p, *, name: str, description: str) -> dict:
    """A space of one's own. The right is the control plane's
    (``can_create_namespace``); everything else — the name's shape, the cap on
    how many one account may own — is enforced where namespaces are created."""
    name = (name or "").strip()
    if not name:
        raise Refused(422, "a space needs a name")
    if len(name) > 100:
        raise Refused(422, "the name is longer than 100 characters")
    description = (description or "").strip()[:500]
    if p.user_id is None:
        raise Refused(409, "the administrator token has no account to own a space")
    # `identity.create_namespace` is an upsert: the same name gives the same
    # space back. That is right for a provisioning script and wrong for a button
    # — someone would click Create and be shown a space they already had.
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM namespace WHERE owner_user_id = %s AND name = %s", (p.user_id, name))
        if cur.fetchone():
            raise Refused(409, "you already have a space with this name")
    try:
        nsid = identity.create_own_namespace(conn, p, name, description=description)
    except identity.SpaceAmbiguous as e:      # the name is taken, or the cap is reached
        raise Refused(409, str(e)) from None
    except identity.AuthError as e:
        raise Refused(403, str(e)) from None
    except ValueError as e:
        raise Refused(422, str(e)) from None
    return {"id": nsid, "name": name}


# ─── asking to join ──────────────────────────────────────────────────────────
def my_request(conn, user_id: str, space_id: str) -> dict:
    """The caller's own request for a space, if any — answered from the caller's
    rows alone, so it says nothing about whether the space exists."""
    space_id = _uuid(space_id)
    with conn.cursor() as cur:
        cur.execute("SELECT status, requested_permission, created_at FROM access_request "
                    "WHERE requester_user_id = %s AND namespace_id = %s", (user_id, space_id))
        row = cur.fetchone()
    if row is None:
        return {"status": None}
    return {"status": row[0], "permission": row[1], "created_at": row[2]}


def ask(conn, p, space_id: str, permission: str) -> dict:
    space_id = _uuid(space_id)
    try:
        return admin.request_access(conn, p, namespace_id=space_id, permission=_perm(permission))
    except ValueError as e:                     # the per-account cap
        raise Refused(409, str(e)) from None


def decide(conn, p, space_id: str, request_id: str, *, approve: bool,
           permission: Optional[str], expect_permission: Optional[str] = None) -> dict:
    """Approve (with the permission the administrator picked, which may differ
    from what was asked) or deny."""
    space_id, request_id = _uuid(space_id), _uuid(request_id)
    _require_admin(conn, p, space_id)
    with conn.cursor() as cur:
        cur.execute("SELECT requester_user_id, requested_permission FROM access_request "
                    "WHERE id = %s AND namespace_id = %s AND status = 'pending' FOR UPDATE",
                    (request_id, space_id))
        row = cur.fetchone()
        if row is None:
            raise Refused(404, "not found")
        requester, asked = str(row[0]), row[1]
        if expect_permission is not None and expect_permission != asked:
            raise Refused(409, "this request changed — look at it again")
        if not approve:
            cur.execute("UPDATE access_request SET status = 'denied', decided_at = now() WHERE id = %s",
                        (request_id,))
            return {"status": "denied"}
        granted = _perm(permission or asked)
        if identity.namespace_owner(conn, space_id) != requester:
            # approving an old request never takes away access given since
            _grant_at_least(cur, space_id, requester, granted)
        cur.execute("UPDATE access_request SET status = 'approved', decided_at = now() WHERE id = %s",
                    (request_id,))
        now_has = identity._reach(cur, requester, space_id)
    return {"status": "approved", "permission": now_has}


# ─── invitations meet a sign-in ──────────────────────────────────────────────
def apply_invites(conn, user_id: str, verified_email: Optional[str]) -> int:
    """Turn open invitations for a provider-verified address into memberships.

    Called only after the sign-in itself was let in. An invitation whose sender
    no longer administers the space (or is switched off) is not applied: it
    carried their authority, and that authority is gone. Nothing is lowered —
    an existing stronger membership stays."""
    if not verified_email:
        return 0
    applied = 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.id, i.namespace_id, i.permission, i.invited_by, n.owner_user_id "
            "FROM space_invite i JOIN namespace n ON n.id = i.namespace_id "
            "WHERE lower(i.email) = lower(%s) AND i.accepted_at IS NULL AND i.expires_at > now() "
            "FOR UPDATE OF i", (verified_email,))
        for iid, nsid, perm, by, owner in cur.fetchall():
            if not _still_may_grant(cur, str(by) if by else None, str(nsid)):
                continue
            if str(owner) != user_id:
                _grant_at_least(cur, str(nsid), user_id, perm)
            cur.execute("UPDATE space_invite SET accepted_at = now(), user_id = %s WHERE id = %s",
                        (user_id, iid))
            applied += 1
    return applied


def _still_may_grant(cur, inviter: Optional[str], space_id: str) -> bool:
    if inviter is None:
        return False
    cur.execute("SELECT role, disabled_at IS NOT NULL FROM app_user WHERE id = %s", (inviter,))
    row = cur.fetchone()
    if row is None or row[1]:
        return False
    if row[0] == "superadmin":
        return True
    return identity._reach(cur, inviter, space_id) == "admin"


def invites_for(conn, email: Optional[str]) -> list:
    """Open invitations for an address — shown next to a sign-in waiting for
    approval, so the administrator knows someone expects this person."""
    if not email:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.name, COALESCE(NULLIF(u.full_name, ''), NULLIF(u.name, ''), u.email), i.permission, "
            "       i.invited_by, i.namespace_id "
            "FROM space_invite i JOIN namespace n ON n.id = i.namespace_id "
            "LEFT JOIN app_user u ON u.id = i.invited_by "
            "WHERE lower(i.email) = lower(%s) AND i.accepted_at IS NULL AND i.expires_at > now() "
            "ORDER BY i.created_at", (email,))
        rows = cur.fetchall()
        # only invitations that would still apply are worth telling an administrator about
        return [{"space": s, "by": by, "permission": perm} for s, by, perm, by_id, nsid in rows
                if _still_may_grant(cur, str(by_id) if by_id else None, str(nsid))]


# ─── HTTP ────────────────────────────────────────────────────────────────────
def mount(app, cfg, pool, panel) -> None:
    from fastapi import Body, HTTPException, Request

    from .routes import _Throttle
    from .sessions import control_principal

    # adding by email is the one place a space administrator learns whether an
    # address has an account here; this keeps it from being run as a lookup
    adds = _Throttle(limit=ADDS_PER_HOUR, window_s=3600)

    def _account(s):
        if s.user_id is None:
            raise HTTPException(409, "the administrator token has no account")
        return s

    def _run(fn):
        with pool.connection() as conn, conn.transaction():
            try:
                return fn(conn)
            except Refused as e:
                raise HTTPException(e.status, str(e))

    @app.post("/ui/api/spaces", status_code=201)
    def make_space(request: Request, name: str = Body(..., embed=True),
                   description: str = Body("", embed=True)):
        s = panel["changing"](request)
        return _run(lambda conn: create_own(conn, control_principal(s), name=name, description=description))

    @app.get("/ui/api/spaces/{space_id}/members")
    def members(space_id: str, request: Request):
        p = control_principal(panel["session"](request))
        return _run(lambda conn: overview(conn, p, space_id))

    @app.post("/ui/api/spaces/{space_id}/members")
    def add(space_id: str, request: Request, email: str = Body(..., embed=True),
            permission: str = Body("read", embed=True)):
        s = panel["changing"](request)
        key = s.user_id or "root"
        if adds.blocked(key):
            raise HTTPException(429, "too many people added in the last hour — try again later")
        adds.fail(key)
        return _run(lambda conn: invite(conn, control_principal(s), space_id, email=email,
                                        permission=permission, inviter_id=s.user_id))

    @app.patch("/ui/api/spaces/{space_id}/members/{user_id}")
    def change(space_id: str, user_id: str, request: Request, permission: str = Body(..., embed=True)):
        p = control_principal(panel["changing"](request))
        _run(lambda conn: set_permission(conn, p, space_id, user_id, permission))
        return {"user_id": user_id, "permission": permission}

    @app.delete("/ui/api/spaces/{space_id}/members/{user_id}")
    def drop(space_id: str, user_id: str, request: Request):
        s = panel["changing"](request)
        _run(lambda conn: remove(conn, control_principal(s), space_id, user_id, actor_id=s.user_id))
        return {"removed": user_id}

    @app.delete("/ui/api/spaces/{space_id}/invites/{invite_id}")
    def uninvite(space_id: str, invite_id: str, request: Request):
        p = control_principal(panel["changing"](request))
        _run(lambda conn: cancel_invite(conn, p, space_id, invite_id))
        return {"cancelled": invite_id}

    @app.post("/ui/api/spaces/{space_id}/transfer")
    def hand_over(space_id: str, request: Request, user_id: str = Body(..., embed=True),
                  keep_me: bool = Body(True, embed=True)):
        p = control_principal(panel["changing"](request))
        return _run(lambda conn: transfer(conn, p, space_id, user_id, keep_me=keep_me))

    @app.get("/ui/api/spaces/{space_id}/access")
    def access_state(space_id: str, request: Request):
        s = _account(panel["session"](request))
        return _run(lambda conn: my_request(conn, s.user_id, space_id))

    @app.post("/ui/api/spaces/{space_id}/access")
    def ask_access(space_id: str, request: Request, permission: str = Body("read", embed=True)):
        s = _account(panel["changing"](request))
        return _run(lambda conn: ask(conn, control_principal(s), space_id, permission))

    @app.post("/ui/api/spaces/{space_id}/requests/{request_id}")
    def decide_request(space_id: str, request_id: str, request: Request,
                       approve: bool = Body(..., embed=True),
                       permission: Optional[str] = Body(None, embed=True),
                       expect_permission: Optional[str] = Body(None, embed=True)):
        p = control_principal(panel["changing"](request))
        return _run(lambda conn: decide(conn, p, space_id, request_id, approve=approve,
                                        permission=permission, expect_permission=expect_permission))
