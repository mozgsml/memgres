"""Control-plane service layer: who may provision what, decided in one place.

`identity` holds the database primitives (create a user, mint a token, add a
member). It deliberately does no authorization — `set_role`'s own docstring
tells callers to guard lockout themselves. Until now each transport supplied
those rules on its own: the HTTP layer grew `require_manage_users` /
`require_superadmin`, the MCP layer grew `_admin_uid`, and the two drifted. A
rule written at one door and forgotten at the other is not a style problem: it
is how the token-escalation hole reached a release.

So this module is to the control plane what `store` is to the data plane — the
one place both doors call. Transports keep only what is genuinely theirs:
parsing a request, mapping an exception to a status code or a tool error,
serializing a datetime. No permission check belongs in a transport.

**Every entry point takes a `Principal`, never a secret.** Authentication
("who are you") stays at the door, where the credential lives — a bearer token
today, a browser session or an OIDC assertion later. Authorization ("what may
you do") lives here and never learns which of those it was. That split is what
lets a web panel authenticate however it likes without touching this file.

Errors are domain-shaped, not HTTP-shaped: `Forbidden` when an authenticated
caller may not do this, `Lockout` when an action would break an invariant that
leaves nobody in charge, and `identity`'s own `AuthError` / `SpaceNotFound` /
`ValueError` straight through. Each door maps them to its own vocabulary.
"""

import datetime as dt
from typing import List, Optional

from . import identity
from .identity import Principal


class Forbidden(PermissionError):
    """Authenticated, but this principal may not perform this action.

    Distinct from `identity.AuthError`, which means the credential itself
    failed (missing, malformed, revoked, expired). The distinction matters at
    the door: one is 401 "prove who you are", the other 403 "you did, and the
    answer is still no".
    """


class Lockout(RuntimeError):
    """Refused because it would leave the deployment with nobody in charge.

    Its own type rather than a reused `AuthError`, because a transport must map
    it to a conflict (the request was well-formed and permitted — the *state*
    forbids it), not to a permission failure.
    """


# ─── authorization predicates ────────────────────────────────────────────────
# The two service tiers. Both take a resolved Principal so they can be reused
# by any door, and both are pure — they raise or return, they never touch the
# database.

def require_manage_users(p: Principal) -> Principal:
    """Provisioning tier: user_manager, superadmin, or the env break-glass root."""
    if not identity.can_manage_users(p):
        raise Forbidden("user management requires user_manager or superadmin")
    return p


def require_superadmin(p: Principal) -> Principal:
    """Root tier: cross-tenant access and handing out service roles."""
    if not p.is_admin:                       # superadmin user or env root
        raise Forbidden("superadmin required")
    return p


def require_namespace_admin(conn, p: Principal, namespace_id: str) -> str:
    """Per-namespace admin: effective permission (membership ∧ token ceiling).

    A third tier, orthogonal to the two above: it is about one namespace, not
    about the deployment. Returns the caller's user id.
    """
    if p.user_id is None:
        raise identity.AuthError("this token has no owning user")
    with conn.cursor() as cur:
        membership = identity._reach(cur, p.user_id, namespace_id)
    if membership is None:
        raise identity.SpaceNotFound("no such namespace")
    if identity.perm_min(membership, p.permission) != "admin":
        raise Forbidden("admin on this namespace required")
    return p.user_id


# ─── users ───────────────────────────────────────────────────────────────────

def create_user(conn, p: Principal, *, name: str = "", description: str = "",
                role: str = "user") -> str:
    """Create a user and return its id.

    Minting an admin-role user is itself a superadmin act: a user_manager must
    not escalate anyone — nor itself, by way of a fresh admin user it then
    issues a token for.
    """
    require_manage_users(p)
    if role != "user" and not p.is_admin:
        raise Forbidden("granting an admin role requires superadmin")
    return identity.create_user(conn, name, description, role=role)


def grant_superadmin(conn, p: Principal, *, user_id: str) -> dict:
    """Promote a user to superadmin."""
    require_superadmin(p)
    identity.grant_superadmin(conn, user_id)
    return {"user_id": user_id, "role": "superadmin"}


def revoke_superadmin(conn, p: Principal, *, user_id: str,
                      demote_to: str = "user") -> dict:
    """Demote a superadmin, refusing to remove the last one."""
    require_superadmin(p)
    try:
        identity.revoke_superadmin(conn, user_id, demote_to=demote_to)
    except identity.AuthError as e:          # anti-lockout, not a permission call
        raise Lockout(str(e))
    return {"user_id": user_id, "role": demote_to}


# ─── namespaces ──────────────────────────────────────────────────────────────

def create_namespace(conn, p: Principal, *, owner_user_id: str, name: str,
                     description: str = "", instruction: str = "") -> str:
    """Create (or return the existing) namespace owned by `owner_user_id`."""
    require_manage_users(p)
    return identity.create_namespace(conn, owner_user_id, name,
                                     description=description,
                                     instruction=instruction)


def add_member(conn, p: Principal, *, namespace_id: str, user_id: str,
               permission: str = "read") -> dict:
    """Share a namespace with another user — cross-tenant, so superadmin only."""
    require_superadmin(p)
    identity.add_member(conn, namespace_id, user_id, permission)
    return {"namespace_id": namespace_id, "user_id": user_id,
            "permission": permission}


def list_spaces(conn, p: Principal) -> List[dict]:
    """Namespaces this principal can reach. Empty for an admin/provisional
    principal, which owns none — it addresses namespaces by id instead."""
    if p.user_id is None:
        return []
    return identity.list_spaces(conn, p.user_id)


# ─── tokens ──────────────────────────────────────────────────────────────────

def issue_token(conn, p: Principal, *, user_id: str,
                namespace_id: Optional[str] = None, permission: str = "write",
                label: str = "", expires_days: Optional[int] = None) -> dict:
    """Mint a token for `user_id`. The secret is returned once and never again.

    `expires_days` rather than a timestamp: both doors were converting the same
    way, so the conversion belongs here.
    """
    require_manage_users(p)
    expires_at = None
    if expires_days:
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expires_days)
    secret, tid = identity.issue_token(conn, user_id, namespace_id=namespace_id,
                                       permission=permission, label=label,
                                       expires_at=expires_at)
    return {"token": secret, "id": tid,
            "note": "store this now — it is not recoverable"}


def revoke_token(conn, p: Principal, *, token_id: str) -> bool:
    """Revoke a token. False if it was already revoked or never existed."""
    require_manage_users(p)
    return identity.revoke_token(conn, token_id)


def list_tokens(conn, p: Principal, *, user_id: str) -> List[dict]:
    """A user's tokens — metadata only, never the secret."""
    require_manage_users(p)
    return identity.list_tokens(conn, user_id)


# ─── access requests: ask to join a namespace, an admin decides ──────────────

def request_access(conn, p: Principal, *, namespace_id: str,
                   permission: str = "read") -> str:
    """Ask for membership. Any authenticated user with an owning account may."""
    if p.user_id is None:
        raise identity.AuthError("this token has no owning user")
    return identity.request_access(conn, p.user_id, namespace_id, permission)


def list_requests(conn, p: Principal, *, namespace_id: str) -> List[dict]:
    """Pending requests for a namespace the caller administers."""
    require_namespace_admin(conn, p, namespace_id)
    return identity.list_requests(conn, namespace_id)


def decide_access(conn, p: Principal, *, request_id: str, approve: bool) -> None:
    """Approve or deny a request, authorized against the namespace it targets."""
    with conn.cursor() as cur:
        cur.execute("SELECT namespace_id FROM access_request WHERE id=%s",
                    (request_id,))
        row = cur.fetchone()
    if row is None:
        raise identity.SpaceNotFound("no such request")
    require_namespace_admin(conn, p, str(row[0]))
    if approve:
        identity.approve_request(conn, request_id)
    else:
        identity.deny_request(conn, request_id)
