"""Identity, tenancy & tokens — payment-agnostic primitives.

memgres knows **nothing** about wallets or payments. It knows users, namespaces,
tokens, memberships and access-requests. A payment/gateway layer is built on top
and is just an ordinary :class:`app_user` that routes.

Two orthogonal axes (never conflate them):

* **organize** memories *within* a space → the tree (`ltree` path) + tags;
* **isolate** between accounts → the **namespace**.

Addressing a space is **id-canonical**: ``namespace.id`` is the unambiguous
address (memberships, ``memory.namespace`` all use it). A *name* is a convenience
that resolves **only against the caller's own** namespaces (``UNIQUE(owner,
name)`` ⇒ no ambiguity); a shared space is reached by id. See
``resolve_space``.

This module is pure DB logic over a psycopg connection — no HTTP, no MCP. The
:class:`~memgres.store.Store` (v0.2, phase 3) calls into it to authenticate and
resolve a space before every operation.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ─── token format: mgk_ + 43 url-safe chars (256-bit) ────────────────────────
TOKEN_RE = re.compile(r"^mgk_[A-Za-z0-9_-]{43}$")


def bearer_token(authorization: Optional[str],
                 x_memgres_token: Optional[str]) -> Optional[str]:
    """Extract a raw memgres token from request auth headers — ``Authorization:
    Bearer <tok>`` first, else the ``X-Memgres-Token`` header — or ``None`` if
    neither is present. One definition for both the HTTP and MCP transports so
    header parsing can't drift between them."""
    if authorization and authorization[:7].lower() == "bearer ":
        return authorization[7:].strip() or None
    if x_memgres_token:
        return x_memgres_token.strip() or None
    return None

# permission lattice
_RANK = {"read": 1, "write": 2, "admin": 3}

# service roles (app_user.role) — orthogonal to the per-namespace permission
# lattice above. `user` is the default; the two admin roles govern the CONTROL
# plane (provisioning) and, for superadmin, cross-namespace data access:
#   user         — owns namespaces, manages access to its OWN spaces only.
#   user_manager — user + create users + (re)issue tokens. No cross-tenant data.
#   superadmin   — full root: read/write any namespace, grant any access,
#                  grant/revoke roles. Principal.is_admin derives from this.
SERVICE_ROLES = ("user", "user_manager", "superadmin")
ADMIN_ROLES = ("user_manager", "superadmin")   # roles that carry any authority


class AuthError(PermissionError):
    """Bad/expired/revoked token, or the token may not do this here."""


class SpaceNotFound(KeyError):
    """No namespace the caller can reach for the given address."""


def new_token() -> str:
    """A fresh server-generated secret: ``mgk_`` + 43 url-safe chars."""
    return "mgk_" + secrets.token_urlsafe(32)


def token_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def valid_format(secret: str) -> bool:
    return bool(secret) and bool(TOKEN_RE.match(secret))


def perm_at_least(have: str, need: str) -> bool:
    return _RANK[have] >= _RANK[need]


def perm_min(a: str, b: str) -> str:
    """The weaker of two permissions (a token ceiling min a membership)."""
    return a if _RANK[a] <= _RANK[b] else b


@dataclass
class Principal:
    """Who a token authenticates as, plus its ceiling and scope.

    ``user_id`` is None for the global admin, and None-but-provisional for a
    valid open-mode token whose user row is created lazily on the first write.
    """
    user_id: Optional[str]
    permission: str                        # token ceiling: read|write|admin
    scope_namespace_id: Optional[str]      # scoped to one ns, or None = all the user's
    token_id: Optional[str] = None
    token_hash: Optional[str] = None
    is_admin: bool = False                 # full root: env break-glass (user_id
                                           # None) or a superadmin-role user
    provisional: bool = False              # valid token, user not yet materialized
    role: str = "user"                     # service role of the owning user


def can_manage_users(p: "Principal") -> bool:
    """May this principal provision users / (re)issue tokens? True for a
    user_manager, a superadmin, or the env break-glass root."""
    return p.is_admin or p.role in ADMIN_ROLES


# ─── authentication ──────────────────────────────────────────────────────────
def resolve(conn, cfg, secret: Optional[str]) -> Principal:
    """Authenticate a bearer secret into a :class:`Principal`.

    * known token → its user/ceiling/scope/role (rejects revoked/expired). A
      token whose user is a superadmin resolves with ``is_admin`` — so once
      bootstrap has stored the env token as a real superadmin's token, that same
      secret authenticates as the *attributed* user, not the anonymous root.
    * env ``admin_token`` (break-glass) → anonymous admin principal. Tried only
      *after* the stored-token lookup, so a seeded env token attributes to its
      user; reachable before the first seed, or in modes bootstrap skips.
    * unknown but well-formed token in ``open`` mode → *provisional* principal
      (user materialized on first write); in ``managed`` mode → rejected.
    """
    if not secret:
        raise AuthError("a token is required")

    h = token_hash(secret)
    if valid_format(secret):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.user_id, t.namespace_id, t.permission, "
                "       (t.revoked_at IS NOT NULL) AS revoked, "
                "       (t.expires_at IS NOT NULL AND t.expires_at <= now()) AS expired, "
                "       u.role "
                "FROM token t JOIN app_user u ON u.id = t.user_id "
                "WHERE t.token_hash=%s", (h,))
            row = cur.fetchone()
            if row is not None:
                tid, uid, nsid, perm, revoked, expired, role = row
                if revoked:
                    raise AuthError("token revoked")
                if expired:
                    raise AuthError("token expired")
                cur.execute("UPDATE token SET last_used_at=now() WHERE id=%s", (tid,))
                return Principal(user_id=str(uid), permission=perm,
                                 scope_namespace_id=str(nsid) if nsid else None,
                                 token_id=str(tid), token_hash=h, role=role,
                                 is_admin=(role == "superadmin"))

    # env break-glass root — any format (an operator may set a non-mgk secret);
    # constant-time compare. Never reached for a seeded env token (matched above).
    if cfg.admin_token and hmac.compare_digest(secret, cfg.admin_token):
        return Principal(user_id=None, permission="admin",
                         scope_namespace_id=None, is_admin=True)

    if not valid_format(secret):
        raise AuthError("malformed token (expected mgk_ + 43 url-safe chars)")
    if cfg.key_mode == "open":
        # accepted, but nothing is created until the first write
        return Principal(user_id=None, permission="write",
                         scope_namespace_id=None, token_hash=h, provisional=True)
    raise AuthError("unknown token")


def ensure_user_for_token(conn, principal: Principal) -> str:
    """Materialize the user + persist the token row for a provisional principal
    (open mode, first write). Mutates ``principal`` in place and returns its id."""
    if principal.user_id is not None:
        return principal.user_id
    if not principal.token_hash:
        raise AuthError("cannot materialize a user without a token")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_user DEFAULT VALUES RETURNING id")
        uid = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO token (token_hash, user_id, permission) "
            "VALUES (%s, %s, 'write') ON CONFLICT (token_hash) DO NOTHING "
            "RETURNING id", (principal.token_hash, uid))
        r = cur.fetchone()
        if r is None:  # lost a race: the token now exists, adopt its user
            cur.execute("SELECT id, user_id, permission, namespace_id "
                        "FROM token WHERE token_hash=%s", (principal.token_hash,))
            tid, existing_uid, perm, nsid = cur.fetchone()
            cur.execute("DELETE FROM app_user WHERE id=%s", (uid,))
            principal.user_id = str(existing_uid)
            principal.token_id = str(tid)
            principal.permission = perm
            principal.scope_namespace_id = str(nsid) if nsid else None
        else:
            principal.token_id = str(r[0])
            principal.user_id = uid
    principal.provisional = False
    return principal.user_id


# ─── space resolution (id-canonical; name = own-only convenience) ────────────
def _reach(cur, user_id: str, namespace_id: str) -> Optional[str]:
    """The caller's effective permission on a namespace, or None if unreachable."""
    cur.execute("SELECT owner_user_id FROM namespace WHERE id=%s", (namespace_id,))
    row = cur.fetchone()
    if row is None:
        return None
    if str(row[0]) == user_id:
        return "admin"
    cur.execute("SELECT permission FROM namespace_member "
                "WHERE namespace_id=%s AND user_id=%s", (namespace_id, user_id))
    m = cur.fetchone()
    return m[0] if m else None


def resolve_space(conn, principal: Principal, *, space_id: Optional[str] = None,
                  space: Optional[str] = None,
                  for_write: bool = False) -> Tuple[str, str]:
    """Resolve an address to ``(namespace_id, effective_permission)``.

    * ``space_id`` — canonical; any reachable space (owned or shared).
    * ``space`` (name) — resolved **only among the caller's own** namespaces.
    * neither — the default namespace (token scope > user default > lazy).

    ``for_write=True`` allows lazy creation (of the user, a named space, or the
    default). Reads never create. The returned permission is the token ceiling
    min the caller's membership; the caller enforces it against the op needed.
    """
    if principal.is_admin and principal.user_id is None:
        # env break-glass root: anonymous, addresses only by id
        if space_id:
            return str(space_id), "admin"
        raise AuthError("global admin must address a space by id")

    if principal.user_id is None:            # provisional (open mode)
        if for_write:
            ensure_user_for_token(conn, principal)
        else:
            raise SpaceNotFound("token has no namespaces yet")
    uid = principal.user_id
    scope = principal.scope_namespace_id
    ceiling = principal.permission

    def _scoped_ok(nsid: str):
        if scope is not None and str(nsid) != scope:
            raise AuthError("token is scoped to a different namespace")

    with conn.cursor() as cur:
        # 1) by id — reach anything owned or shared; a superadmin user reaches
        #    ANY space by id (full root), still capped by its token ceiling.
        if space_id is not None:
            _scoped_ok(space_id)
            perm = _reach(cur, uid, str(space_id))
            if perm is None:
                if principal.is_admin:
                    return str(space_id), perm_min("admin", ceiling)
                raise SpaceNotFound(f"namespace {space_id} not reachable")
            return str(space_id), perm_min(perm, ceiling)

        # 2) by name — own namespaces only (no ambiguity with shared ones)
        if space is not None:
            cur.execute("SELECT id FROM namespace WHERE owner_user_id=%s AND name=%s",
                        (uid, space))
            row = cur.fetchone()
            if row is not None:
                _scoped_ok(row[0])
                return str(row[0]), perm_min("admin", ceiling)
            if for_write:
                if scope is not None:
                    raise AuthError("a scoped token cannot create a new namespace")
                nsid = create_namespace(conn, uid, space)
                return nsid, perm_min("admin", ceiling)
            raise SpaceNotFound(f"you own no namespace named '{space}'")

        # 3) default resolution
        if scope is not None:
            # a scoped token resolves to its namespace — but only if the user can
            # actually reach it. Never default an unreachable scope to "read"
            # (that would leak a namespace the token was wrongly scoped to).
            perm = _reach(cur, uid, scope)
            if perm is None:
                raise SpaceNotFound(f"token scope {scope} not reachable")
            return scope, perm_min(perm, ceiling)
        cur.execute("SELECT default_namespace_id FROM app_user WHERE id=%s", (uid,))
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0]), perm_min("admin", ceiling)
        if for_write:
            nsid = ensure_default_namespace(conn, uid)
            return nsid, perm_min("admin", ceiling)
        raise SpaceNotFound("user has no default namespace yet")


# ─── management: users / namespaces / members ────────────────────────────────
def create_user(conn, name: str = "", description: str = "",
                role: str = "user") -> str:
    if role not in SERVICE_ROLES:
        raise ValueError(f"bad role: {role}")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_user (name, description, role) "
                    "VALUES (%s, %s, %s) RETURNING id", (name, description, role))
        return str(cur.fetchone()[0])


# ─── service roles (control plane; see SERVICE_ROLES) ────────────────────────
def count_service_admins(conn) -> int:
    """How many users hold an admin role (user_manager or superadmin). Zero ⇒ a
    fresh install with no control plane — the trigger for bootstrap seeding."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app_user WHERE role IN "
                    "('user_manager','superadmin')")
        return int(cur.fetchone()[0])


def count_superadmins(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app_user WHERE role='superadmin'")
        return int(cur.fetchone()[0])


def get_role(conn, user_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM app_user WHERE id=%s", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


def set_role(conn, user_id: str, role: str) -> None:
    """Set a user's service role directly. Callers that lower a superadmin must
    guard against lockout themselves; :func:`revoke_superadmin` does that."""
    if role not in SERVICE_ROLES:
        raise ValueError(f"bad role: {role}")
    with conn.cursor() as cur:
        cur.execute("UPDATE app_user SET role=%s WHERE id=%s", (role, user_id))
        if cur.rowcount == 0:
            raise SpaceNotFound(f"no such user {user_id}")


def grant_superadmin(conn, user_id: str) -> None:
    set_role(conn, user_id, "superadmin")


def revoke_superadmin(conn, user_id: str, *, demote_to: str = "user") -> None:
    """Drop a user out of the superadmin role. Anti-lockout: refuses to remove
    the **last** superadmin (recover such a lockout via the grant CLI)."""
    if demote_to not in SERVICE_ROLES or demote_to == "superadmin":
        raise ValueError(f"bad demote target: {demote_to}")
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM app_user WHERE id=%s", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise SpaceNotFound(f"no such user {user_id}")
        if row[0] != "superadmin":
            return                        # nothing to revoke
        cur.execute("SELECT count(*) FROM app_user WHERE role='superadmin'")
        if int(cur.fetchone()[0]) <= 1:
            raise AuthError("cannot revoke the last superadmin")
        cur.execute("UPDATE app_user SET role=%s WHERE id=%s", (demote_to, user_id))


def create_namespace(conn, owner_user_id: str, name: str, *,
                     description: str = "", instruction: str = "") -> str:
    """Create (or return the existing) namespace ``name`` owned by the user."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO namespace (owner_user_id, name, description, instruction) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (owner_user_id, name) DO NOTHING "
            "RETURNING id", (owner_user_id, name, description, instruction))
        row = cur.fetchone()
        if row is not None:
            return str(row[0])
        cur.execute("SELECT id FROM namespace WHERE owner_user_id=%s AND name=%s",
                    (owner_user_id, name))
        return str(cur.fetchone()[0])


def ensure_default_namespace(conn, user_id: str, name: str = "default") -> str:
    """Return the user's default namespace, creating one named ``name`` if unset."""
    with conn.cursor() as cur:
        cur.execute("SELECT default_namespace_id FROM app_user WHERE id=%s", (user_id,))
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    nsid = create_namespace(conn, user_id, name)
    set_default_space(conn, user_id, nsid)
    return nsid


def set_default_space(conn, user_id: str, namespace_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE app_user SET default_namespace_id=%s WHERE id=%s",
                    (namespace_id, user_id))


def add_member(conn, namespace_id: str, user_id: str, permission: str = "read") -> None:
    if permission not in _RANK:
        raise ValueError(f"bad permission: {permission}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO namespace_member (namespace_id, user_id, permission) "
            "VALUES (%s, %s, %s) ON CONFLICT (namespace_id, user_id) "
            "DO UPDATE SET permission=EXCLUDED.permission",
            (namespace_id, user_id, permission))


def edit_namespace(conn, namespace_id: str, *, description: Optional[str] = None,
                   instruction: Optional[str] = None) -> None:
    sets, params = [], []
    if description is not None:
        sets.append("description=%s"); params.append(description)
    if instruction is not None:
        sets.append("instruction=%s"); params.append(instruction)
    if not sets:
        return
    params.append(namespace_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE namespace SET {', '.join(sets)} WHERE id=%s", params)


def list_spaces(conn, user_id: str) -> List[dict]:
    """Every namespace the user can reach: owned + shared, with flags."""
    with conn.cursor() as cur:
        cur.execute("SELECT default_namespace_id FROM app_user WHERE id=%s", (user_id,))
        r = cur.fetchone()
        default = str(r[0]) if r and r[0] else None
        cur.execute(
            "SELECT n.id, n.name, n.description, n.instruction, n.owner_user_id, "
            "       CASE WHEN n.owner_user_id=%(u)s THEN 'admin' ELSE m.permission END "
            "FROM namespace n "
            "LEFT JOIN namespace_member m ON m.namespace_id=n.id AND m.user_id=%(u)s "
            "WHERE n.owner_user_id=%(u)s OR m.user_id=%(u)s "
            "ORDER BY n.created_at", {"u": user_id})
        out = []
        for nid, name, desc, instr, owner, perm in cur.fetchall():
            out.append({
                "id": str(nid), "name": name, "description": desc,
                "instruction": instr, "permission": perm,
                "mine": str(owner) == user_id,
                "is_default": str(nid) == default,
            })
        return out


# ─── management: tokens ──────────────────────────────────────────────────────
def issue_token(conn, user_id: str, *, namespace_id: Optional[str] = None,
                permission: str = "write", label: str = "",
                expires_at=None) -> Tuple[str, str]:
    """Mint a token for a user. Returns ``(secret, token_id)``; the secret is
    shown once and stored only as a hash. ``namespace_id`` scopes it to one
    space; ``permission`` is its ceiling."""
    if permission not in _RANK:
        raise ValueError(f"bad permission: {permission}")
    secret = new_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO token (token_hash, user_id, namespace_id, permission, "
            "label, expires_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (token_hash(secret), user_id, namespace_id, permission, label, expires_at))
        return secret, str(cur.fetchone()[0])


def register_token(conn, user_id: str, secret: str, *,
                   namespace_id: Optional[str] = None, permission: str = "write",
                   label: str = "", expires_at=None) -> str:
    """Store a bring-your-own token (open mode). Validates format, hashes it."""
    if not valid_format(secret):
        raise ValueError("bring-your-own token must match mgk_ + 43 url-safe chars")
    if permission not in _RANK:
        raise ValueError(f"bad permission: {permission}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO token (token_hash, user_id, namespace_id, permission, "
            "label, expires_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (token_hash(secret), user_id, namespace_id, permission, label, expires_at))
        return str(cur.fetchone()[0])


def revoke_token(conn, token_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("UPDATE token SET revoked_at=now() "
                    "WHERE id=%s AND revoked_at IS NULL", (token_id,))
        return cur.rowcount > 0


def token_owner(conn, token_id: str) -> Optional[str]:
    """Which user a token belongs to, or None if there is no such token.

    Needed to authorize an action addressed by *token* rather than by user: the
    caller's right to touch it follows from whose token it is.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM token WHERE id=%s", (token_id,))
        row = cur.fetchone()
    return str(row[0]) if row else None


def list_tokens(conn, user_id: str) -> List[dict]:
    """A user's tokens — metadata only, never the secret."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, namespace_id, permission, label, expires_at, revoked_at, "
            "       last_used_at, created_at FROM token WHERE user_id=%s "
            "ORDER BY created_at", (user_id,))
        cols = ["id", "namespace_id", "permission", "label", "expires_at",
                "revoked_at", "last_used_at", "created_at"]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["id"] = str(d["id"])
            d["namespace_id"] = str(d["namespace_id"]) if d["namespace_id"] else None
            d["active"] = d["revoked_at"] is None
            out.append(d)
        return out


# ─── request-access: join an existing namespace ──────────────────────────────
def request_access(conn, requester_user_id: str, namespace_id: str,
                   permission: str = "read") -> str:
    if permission not in _RANK:
        raise ValueError(f"bad permission: {permission}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO access_request (requester_user_id, namespace_id, "
            "requested_permission) VALUES (%s, %s, %s) "
            "ON CONFLICT (requester_user_id, namespace_id) DO UPDATE "
            "SET requested_permission=EXCLUDED.requested_permission, "
            "    status='pending', decided_at=NULL RETURNING id",
            (requester_user_id, namespace_id, permission))
        return str(cur.fetchone()[0])


def list_requests(conn, namespace_id: str, *, pending_only: bool = True) -> List[dict]:
    q = ("SELECT id, requester_user_id, requested_permission, status, created_at, "
         "decided_at FROM access_request WHERE namespace_id=%s")
    if pending_only:
        q += " AND status='pending'"
    q += " ORDER BY created_at"
    with conn.cursor() as cur:
        cur.execute(q, (namespace_id,))
        cols = ["id", "requester_user_id", "requested_permission", "status",
                "created_at", "decided_at"]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["id"] = str(d["id"])
            d["requester_user_id"] = str(d["requester_user_id"])
            out.append(d)
        return out


def approve_request(conn, request_id: str) -> None:
    """Grant the requested membership and close the request."""
    with conn.cursor() as cur:
        cur.execute("SELECT requester_user_id, namespace_id, requested_permission "
                    "FROM access_request WHERE id=%s AND status='pending'",
                    (request_id,))
        row = cur.fetchone()
        if row is None:
            raise SpaceNotFound(f"no pending request {request_id}")
        requester, nsid, perm = row
    add_member(conn, str(nsid), str(requester), perm)
    with conn.cursor() as cur:
        cur.execute("UPDATE access_request SET status='approved', decided_at=now() "
                    "WHERE id=%s", (request_id,))


def deny_request(conn, request_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE access_request SET status='denied', decided_at=now() "
                    "WHERE id=%s AND status='pending'", (request_id,))
