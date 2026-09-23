"""Browser sessions for the web panel — pure database logic, no HTTP.

A session is how a browser proves who it is between requests. It is NOT a new
kind of authority: every request re-derives the caller from the session's
account (role, disabled, and — for a token sign-in — the token itself), so a
session never outlives the thing that justified it. Revoke the token, disable
the account, demote the admin: the next request is refused, not the next
sign-in.

What the panel may do with memory is decided elsewhere, by the ordinary
namespace rules, through a :class:`~memgres.identity.Principal` built here with
a READ ceiling. The first panel only reads memory; if a bug ever routed a write
through it, the ceiling refuses it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Optional

from .. import identity
from ..identity import ADMIN_ROLES, AuthError, Principal

# last_seen_at is written at most this often, so an idle panel polling its
# session does not turn every read into a write.
_TOUCH_EVERY_S = 60


def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode("ascii")).hexdigest()


@dataclass
class Session:
    id_hash: str
    user_id: Optional[str]          # None = env break-glass root
    via: str                        # 'token' | 'oidc:<provider>'
    token_id: Optional[str]
    csrf: str
    expires_at: object
    role: str = "user"


# ─── sign-in ─────────────────────────────────────────────────────────────────
def authenticate_admin_token(conn, cfg, secret: str) -> Principal:
    """Check a secret offered at the administrator sign-in.

    Accepted: the deployment's ``MEMGRES_ADMIN_TOKEN`` (however it resolves — to
    the seeded admin account, or to the anonymous root), or a personal token of
    an account whose role carries authority. Everyone else signs in through a
    provider; a plain user's token is refused here even though it is valid,
    because this door exists for the day no provider works.

    Raises :class:`~memgres.identity.AuthError` with one message for every
    refusal, so the answer does not say whether a token exists.
    """
    refused = AuthError("this token cannot sign in here")
    if not secret or len(secret) > 512:
        raise refused
    try:
        # touch=False: being refused here is not "using" the token
        p = identity.resolve(conn, cfg, secret, touch=False)
    except AuthError:
        raise refused from None
    if p.provisional:
        raise refused
    if not (p.is_admin or p.role in ADMIN_ROLES):
        raise refused
    # The token's own limits count, not only the account's role. A read-only
    # token pinned to one space, minted for an agent, must not open a session
    # with the account's whole authority — from which it could mint an unpinned
    # token or link a sign-in method and outlive its own revocation. The REST
    # control plane refuses such a credential for the same reason
    # (admin._require_full_credential).
    if p.permission != "admin" or p.scope_namespace_id is not None:
        raise refused
    return p


def create(conn, cfg, p: Principal, *, via: str, identity_id: Optional[str] = None) -> tuple:
    """Open a session for an authenticated principal. Returns ``(sid, csrf)``;
    only the sid's hash is stored."""
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    root_fp = None
    if p.user_id is None:
        # The anonymous root holds no account to re-check, so the session is
        # bound to the secret itself: rotate the env token and it ends.
        if not cfg.admin_token:
            raise AuthError("this token cannot sign in here")
        root_fp = identity.token_hash(cfg.admin_token)
    with conn.cursor() as cur:
        # sessions that ended a day ago are only clutter
        cur.execute("DELETE FROM web_session WHERE expires_at < now() - interval '1 day' "
                    "OR revoked_at < now() - interval '1 day'")
        cur.execute(
            "INSERT INTO web_session (id_hash, user_id, via, token_id, root_fp, csrf, identity_id, "
            "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(hours => %s))",
            (_hash(sid), p.user_id, via, p.token_id, root_fp, csrf, identity_id,
             cfg.web_session_hours))
    return sid, csrf


# ─── every request ───────────────────────────────────────────────────────────
def load(conn, cfg, sid: Optional[str]) -> Optional[Session]:
    """The live session a cookie names, or None.

    None covers every way a session can have stopped being valid — unknown,
    ended, expired, or its justification gone — and the caller treats them all
    as "not signed in". Telling them apart would only help someone probing ids.
    """
    if not sid or len(sid) > 128:
        return None
    return load_by_hash(conn, cfg, _hash(sid))


def session_hash(sid: str) -> str:
    return _hash(sid)


def load_by_hash(conn, cfg, id_hash: Optional[str]) -> Optional[Session]:
    """:func:`load`, for a session known by its stored hash (an OIDC link flow
    records the hash of the session that started it)."""
    if not id_hash:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.id_hash, s.user_id, s.via, s.token_id, s.root_fp, s.csrf, "
            "       s.expires_at, EXTRACT(EPOCH FROM now() - s.last_seen_at), "
            "       u.role, (u.disabled_at IS NOT NULL), "
            "       (t.id IS NOT NULL), (t.revoked_at IS NOT NULL), "
            "       (t.expires_at IS NOT NULL AND t.expires_at <= now()) "
            "FROM web_session s "
            "LEFT JOIN app_user u ON u.id = s.user_id "
            "LEFT JOIN token t ON t.id = s.token_id "
            "WHERE s.id_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()",
            (id_hash,))
        row = cur.fetchone()
        if row is None:
            return None
        (id_hash, uid, via, tid, root_fp, csrf, expires, idle_s,
         role, disabled, has_token, t_revoked, t_expired) = row

        if uid is None:
            if not (root_fp and cfg.admin_token and hmac.compare_digest(
                    root_fp, identity.token_hash(cfg.admin_token))):
                return None
            role = "superadmin"
        else:
            if disabled:
                return None
            if tid is not None:
                # A token sign-in lives exactly as long as the token does, and
                # only while the account still carries the authority that let
                # it through that door.
                if not has_token or t_revoked or t_expired:
                    return None
                if role not in ADMIN_ROLES:
                    return None

        if idle_s is not None and idle_s > _TOUCH_EVERY_S:
            cur.execute("UPDATE web_session SET last_seen_at = now() "
                        "WHERE id_hash = %s", (id_hash,))
    return Session(id_hash=id_hash, user_id=str(uid) if uid else None, via=via,
                   token_id=str(tid) if tid else None, csrf=csrf,
                   expires_at=expires, role=role or "user")


def end(conn, sid: Optional[str]) -> None:
    if not sid or len(sid) > 128:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE web_session SET revoked_at = now() "
                    "WHERE id_hash = %s AND revoked_at IS NULL", (_hash(sid),))


def csrf_ok(session: Session, offered: Optional[str]) -> bool:
    return bool(offered) and hmac.compare_digest(session.csrf, offered)


def principal(session: Session) -> Principal:
    """The caller, for everything the panel asks of memory.

    The ceiling is READ: this release of the panel only looks. Service authority
    (the role) is carried as-is, because it was re-checked when the session
    loaded.
    """
    if session.user_id is None:
        return Principal(user_id=None, permission="read", scope_namespace_id=None,
                         is_admin=True, role="superadmin")
    return Principal(user_id=session.user_id, permission="read",
                     scope_namespace_id=None, role=session.role,
                     is_admin=session.role == "superadmin")


def control_principal(session: Session) -> Principal:
    """The caller, for the control plane: who is in a space, people, roles.

    Unlike :func:`principal` this carries an ``admin`` ceiling and no scope —
    what :mod:`memgres.admin` requires of a credential before it lets anyone
    manage anything. That is what a session is: it opens only through a
    provider (the account itself) or through an admin-ceiling, unscoped token
    (see :func:`authenticate_admin_token`), never through a weakened one. What
    the ACCOUNT may do — its role, its membership of a space, the target's
    role — is still decided by the control plane's own checks.

    Memory is never read or written with this principal.
    """
    if session.user_id is None:
        return Principal(user_id=None, permission="admin", scope_namespace_id=None,
                         is_admin=True, role="superadmin")
    return Principal(user_id=session.user_id, permission="admin",
                     scope_namespace_id=None, role=session.role,
                     is_admin=session.role == "superadmin")


# ─── the administrator door ──────────────────────────────────────────────────
def admin_has_linked_signin(conn) -> bool:
    """Does any active administrator have a way to sign in other than a token?

    While none does, ``/signin`` sends everyone to ``/signin/admin`` — on a
    fresh server that is the only way in, and a sign-in page listing providers
    nobody can use yet would be a dead end.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM app_user_identity i "
            "JOIN app_user u ON u.id = i.user_id "
            "WHERE u.role IN ('user_manager', 'superadmin') AND u.disabled_at IS NULL)")
        return bool(cur.fetchone()[0])


def profile(conn, user_id: Optional[str]) -> Optional[dict]:
    """What the panel shows about the signed-in person."""
    if user_id is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, full_name, email, role, ui_language, ui_colors "
                    "FROM app_user WHERE id = %s", (user_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "name": row[1], "full_name": row[2], "email": row[3],
            "role": row[4], "ui_language": row[5], "ui_colors": row[6] or {}}


MAX_COLOURS = 200          # a person looking at more branches than this is
                           # not choosing colours, and neither is whatever is
                           # calling the endpoint


def set_colour(conn, user_id: str, key: str, colour: Optional[str],
               allowed: tuple) -> dict:
    """Remember (or forget, with ``colour=None``) one person's colour for one
    space or branch. Returns the whole map, so the caller never has to guess
    what it now holds.

    The key is opaque here on purpose — it is a namespace id, optionally with a
    branch after a colon — because validating that a branch still exists would
    make a preference depend on memory that may be renamed or deleted. A key
    that stops meaning anything simply stops being read; the cap is what keeps
    the column from growing without bound.
    """
    from psycopg.types.json import Json

    if colour is not None and colour not in allowed:
        raise ValueError(f"unknown colour {colour!r}")
    if len(key) > 200:
        raise ValueError("that key is too long to be a space or a branch")
    with conn.cursor() as cur:
        cur.execute("SELECT ui_colors FROM app_user WHERE id = %s FOR UPDATE",
                    (user_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError("no such account")
        colours = dict(row[0] or {})
        if colour is None:
            colours.pop(key, None)
        else:
            if key not in colours and len(colours) >= MAX_COLOURS:
                raise ValueError(
                    f"that is {MAX_COLOURS} colours already — clear some first")
            colours[key] = colour
        cur.execute("UPDATE app_user SET ui_colors = %s WHERE id = %s",
                    (Json(colours), user_id))
    return colours


def set_language(conn, user_id: str, lang: Optional[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE app_user SET ui_language = %s WHERE id = %s",
                    (lang, user_id))
