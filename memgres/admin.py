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

def _require_full_credential(p: Principal, action: str) -> None:
    """The control plane needs a credential that has not been deliberately weakened.

    A role says who someone IS; a token says what THIS credential may do — its
    permission ceiling and, optionally, the single namespace it is pinned to.
    The two are independent, and gating the control plane on the role alone made
    the token's limits decorative: a read-only, namespace-scoped token minted for
    an agent still opened the whole control plane, because the *account* behind
    it happened to hold an admin role. From there the weak credential simply
    issued itself a strong one, so pinning an agent to one namespace with a
    read-only token — exactly what the docs recommend — protected nothing.

    Deployment-wide acts therefore require an unscoped, admin-ceiling token. This
    is the rule the self-service token tools already applied; the point of a
    shared service layer is that the stricter door does not stay the exception.
    """
    if p.permission != "admin":
        raise Forbidden(f"{action} requires an admin-ceiling token "
                        f"(this one grants {p.permission})")
    if p.scope_namespace_id is not None:
        raise Forbidden(f"{action} requires an unscoped token — this one is "
                        f"pinned to a single namespace")


def require_manage_users(p: Principal) -> Principal:
    """Provisioning tier: user_manager, superadmin, or the env break-glass root."""
    if not identity.can_manage_users(p):
        raise Forbidden("user management requires user_manager or superadmin")
    _require_full_credential(p, "user management")
    return p


def require_superadmin(p: Principal) -> Principal:
    """Root tier: cross-tenant access and handing out service roles."""
    if not p.is_admin:                       # superadmin user or env root
        raise Forbidden("superadmin required")
    _require_full_credential(p, "this operation")
    return p


def _require_target_is_plain_user(conn, p: Principal, user_id: Optional[str],
                                  action: str) -> None:
    """A user_manager may only act on accounts that carry no authority.

    Provisioning is gated on the caller's role but was not gated on the
    *target's*: a user_manager could mint itself a token for the superadmin's
    account (instant full data root), revoke the last superadmin's token
    (control-plane lockout, recoverable only from the database), or read an
    admin's token metadata. The whole point of the middle tier is that it hands
    out access without gaining it, so touching an admin account is a superadmin
    act.

    An unknown `user_id` is deliberately not an error here — it falls through to
    the primitive, which fails on the foreign key exactly as it did before. This
    check adds a refusal; it does not change what a bad id does.
    """
    if p.is_admin:
        return
    if identity.get_role(conn, user_id) in identity.ADMIN_ROLES:
        raise Forbidden(f"{action} for an admin-role account requires superadmin")


def require_namespace_admin(conn, p: Principal, namespace_id: str) -> Optional[str]:
    """Per-namespace admin: effective permission (membership ∧ token ceiling).

    A third tier, orthogonal to the two above: it is about one namespace, not
    about the deployment. Returns the caller's user id (None for the env root).

    A superadmin passes without membership. `identity._reach` only knows about
    ownership and the member table, so without this a superadmin — defined as
    "read/write any namespace, grant any access" — was refused on a namespace it
    had just provisioned for someone else.
    """
    # A scoped token is pinned to one namespace, and administering a DIFFERENT
    # one is outside what it was issued for — including for a superadmin, whose
    # role would otherwise make the pin meaningless.
    if (p.scope_namespace_id is not None
            and str(p.scope_namespace_id) != str(namespace_id)):
        raise Forbidden("this token is scoped to a different namespace")
    if p.is_admin:
        return p.user_id
    if p.user_id is None:
        raise identity.AuthError("this token has no owning user")
    with conn.cursor() as cur:
        membership = identity._reach(cur, p.user_id, namespace_id)
    if membership is None:
        raise identity.SpaceNotFound("no such namespace")
    if identity.perm_min(membership, p.permission) != "admin":
        raise Forbidden("admin on this namespace required")
    return p.user_id


def _has_full_credential(p: Principal) -> bool:
    """The boolean form of :func:`_require_full_credential` — for reporting what
    a caller may do, where the refusal itself is not wanted."""
    return p.permission == "admin" and p.scope_namespace_id is None


def capabilities(conn, p: Principal) -> dict:
    """What this principal may actually do, right now, with THIS credential.

    Every value is EFFECTIVE, not aspirational: a superadmin holding a read-only
    or namespace-scoped token cannot manage users with it, and this says so —
    the same answer `_require_full_credential` would give. Reporting the role's
    potential instead would send a caller (or a rendered UI) at a door that is
    going to refuse it, which is the drift the service layer exists to prevent.

    `is_admin` is the exception and stays the plain role fact: per-namespace
    authorization consults the role, not the credential, so a name that means
    "the role" has to remain available.
    """
    return {
        "is_admin": p.is_admin,
        "can_manage_users": (identity.can_manage_users(p)
                             and _has_full_credential(p)),
        "can_administer_deployment": p.is_admin and _has_full_credential(p),
        "can_create_namespace": identity.can_create_namespace(conn, p),
        "can_write": identity.perm_at_least(p.permission, "write"),
        "can_manage_own_tokens": _has_full_credential(p),
        # Administering ONE namespace (edit it, list its members) needs an
        # admin-ceiling credential but not an unscoped one — a token pinned to
        # the namespace it administers is exactly the right shape. Whether the
        # caller is admin ON a given namespace still depends on that namespace.
        "has_admin_ceiling": p.permission == "admin",
    }


def whoami(conn, p: Principal) -> dict:
    """Who the caller is and what they may do — capabilities, not just a role.

    A caller has to decide what to attempt: an agent whether to try a tool, a
    web panel which controls to render. If it were handed a bare role it would
    have to re-derive the rules — a second copy of the authorization logic, in
    another language, drifting from this one. So the answers are computed here,
    by the same predicates that enforce them.

    Never echoes the credential; `token_id` identifies it without revealing it.
    """
    return {
        "user_id": p.user_id,
        "role": p.role,
        "permission": p.permission,          # this credential's ceiling
        "scope_namespace_id": p.scope_namespace_id,
        "token_id": p.token_id,
        "capabilities": capabilities(conn, p),
    }


# ─── users ───────────────────────────────────────────────────────────────────

def create_user(conn, p: Principal, *, name: str = "", description: str = "",
                role: str = "user", can_create_namespace: bool = False,
                **profile) -> str:
    """Create a user and return its id.

    Minting an admin-role user is itself a superadmin act: a user_manager must
    not escalate anyone — nor itself, by way of a fresh admin user it then
    issues a token for.

    The new user owns nothing. Give it a namespace, share one with it, or let it
    make its own — deciding that here, rather than creating one automatically,
    is what keeps a provisioned account from quietly acquiring a second store.
    """
    require_manage_users(p)
    if role != "user" and not p.is_admin:
        raise Forbidden("granting an admin role requires superadmin")
    return identity.create_user(conn, name, description, role=role,
                                can_create_namespace=can_create_namespace,
                                **profile)


def edit_user(conn, p: Principal, *, user_id: str, **profile) -> dict:
    """Change who a user is — email, full name, department, position.

    Provisioning-tier, and refused on an admin-role account for a user_manager
    like every other operation that takes a target: an authorship line is what
    an audit reads, so being able to rewrite whose it looks like is authority.
    """
    require_manage_users(p)
    _require_target_is_plain_user(conn, p, user_id, "editing a profile")
    identity.edit_user(conn, user_id, **profile)
    return {"user_id": user_id}


def set_can_create_namespace(conn, p: Principal, *, user_id: str,
                             allowed: bool) -> dict:
    """Grant or withdraw a user's right to create namespaces."""
    require_manage_users(p)
    _require_target_is_plain_user(conn, p, user_id, "changing namespace rights")
    identity.set_can_create_namespace(conn, user_id, allowed)
    return {"user_id": user_id, "can_create_namespace": allowed}


def list_users(conn, p: Principal, *, role: Optional[str] = None,
               limit: Optional[int] = None, offset: int = 0) -> List[dict]:
    """The user directory — needed to address anyone by id at all."""
    require_manage_users(p)
    return identity.list_users(conn, role=role, limit=limit, offset=offset)


def set_role(conn, p: Principal, *, user_id: str, role: str) -> dict:
    """Set a user's service role — the general form of promote and demote.

    `identity.set_role` writes the column and, by its own docstring, leaves
    lockout to the caller. Demoting a superadmin therefore goes through
    `revoke_superadmin`, which counts the remaining ones: without that detour a
    superadmin could demote the last superadmin and leave the deployment with no
    control plane at all.

    It is also the only way to reach `user_manager`. Grant and revoke handle the
    superadmin role alone, so the middle tier was unreachable through any door.
    """
    require_superadmin(p)
    if role not in identity.SERVICE_ROLES:
        raise ValueError(f"bad role: {role}")
    if identity.get_role(conn, user_id) == "superadmin" and role != "superadmin":
        try:
            identity.revoke_superadmin(conn, user_id, demote_to=role)
        except identity.AuthError as e:      # anti-lockout, not a permission call
            raise Lockout(str(e))
    else:
        identity.set_role(conn, user_id, role)   # SpaceNotFound if no such user
    return {"user_id": user_id, "role": role}


def grant_superadmin(conn, p: Principal, *, user_id: str) -> dict:
    """Promote a user to superadmin."""
    return set_role(conn, p, user_id=user_id, role="superadmin")


def revoke_superadmin(conn, p: Principal, *, user_id: str,
                      demote_to: str = "user") -> dict:
    """Demote a superadmin, refusing to remove the last one.

    A no-op on a user who is not a superadmin — but it reports the role the user
    actually has now, rather than echoing the requested one back as though the
    demotion had happened.
    """
    require_superadmin(p)
    if demote_to not in identity.SERVICE_ROLES or demote_to == "superadmin":
        raise ValueError(f"bad demote target: {demote_to}")
    try:
        identity.revoke_superadmin(conn, user_id, demote_to=demote_to)
    except identity.AuthError as e:          # anti-lockout, not a permission call
        raise Lockout(str(e))
    return {"user_id": user_id, "role": identity.get_role(conn, user_id)}


# ─── namespaces ──────────────────────────────────────────────────────────────

def create_namespace(conn, p: Principal, *, owner_user_id: str, name: str,
                     description: str = "", instruction: str = "") -> str:
    """Create (or return the existing) namespace owned by `owner_user_id`."""
    require_manage_users(p)
    return identity.create_namespace(conn, owner_user_id, name,
                                     description=description,
                                     instruction=instruction)


def edit_namespace(conn, p: Principal, *, namespace_id: str,
                   description: Optional[str] = None,
                   instruction: Optional[str] = None) -> dict:
    """Amend a namespace's description or routing instruction.

    `create_namespace` is an idempotent upsert that ignores conflicts, so
    re-creating with corrected text silently does nothing — until now a typo in
    the instruction agents read to choose a namespace could not be fixed through
    any door at all.
    """
    require_namespace_admin(conn, p, namespace_id)
    identity.edit_namespace(conn, namespace_id, description=description,
                            instruction=instruction)
    return {"namespace_id": namespace_id}


def add_member(conn, p: Principal, *, namespace_id: str, user_id: str,
               permission: str = "read") -> dict:
    """Share a namespace with another user — cross-tenant, so superadmin only."""
    require_superadmin(p)
    identity.add_member(conn, namespace_id, user_id, permission)
    return {"namespace_id": namespace_id, "user_id": user_id,
            "permission": permission}


def list_namespaces(conn, p: Principal, *, owner_user_id: Optional[str] = None,
                    limit: Optional[int] = None, offset: int = 0) -> List[dict]:
    """Deployment-wide namespace inventory.

    Provisioning tier rather than superadmin: scoping a token to a namespace
    needs its id, so a user_manager cannot do its job without this. It returns
    namespace *metadata* — names and routing hints — never the memories inside,
    which stay behind the data-plane check.
    """
    require_manage_users(p)
    return identity.list_namespaces(conn, owner_user_id=owner_user_id,
                                    limit=limit, offset=offset)


def list_members(conn, p: Principal, *, namespace_id: str) -> List[dict]:
    """Who can reach a namespace. Authorized per-namespace, not deployment-wide:
    its own admin is exactly who should be able to audit access to it."""
    require_namespace_admin(conn, p, namespace_id)
    return identity.list_members(conn, namespace_id)


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
    _require_target_is_plain_user(conn, p, user_id, "issuing a token")
    expires_at = None
    if expires_days:
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expires_days)
    secret, tid = identity.issue_token(conn, user_id, namespace_id=namespace_id,
                                       permission=permission, label=label,
                                       expires_at=expires_at)
    return {"token": secret, "id": tid,
            "note": "store this now — it is not recoverable"}


def revoke_token(conn, p: Principal, *, token_id: str) -> bool:
    """Revoke a token. False if it was already revoked or never existed.

    Addressed by token, so the target account is whoever owns it.
    """
    require_manage_users(p)
    _require_target_is_plain_user(conn, p, identity.token_owner(conn, token_id),
                                  "revoking a token")
    return identity.revoke_token(conn, token_id)


def list_tokens(conn, p: Principal, *, user_id: str) -> List[dict]:
    """A user's tokens — metadata only, never the secret."""
    require_manage_users(p)
    _require_target_is_plain_user(conn, p, user_id, "listing tokens")
    return identity.list_tokens(conn, user_id)


# ─── access requests: ask to join a namespace, an admin decides ──────────────

# ─── adopting data left behind by single mode ───────────────────────────────
# In `single` mode every memory is stored under the namespace `''`. Switching to
# open/managed does not move them, and no principal ever resolves to `''`, so the
# whole corpus becomes unreachable — present, unharmed, and invisible. These two
# make that state visible and then fixable.
ORPHAN_NAMESPACE = ""


def count_orphans(conn, p: Principal) -> dict:
    """How many memories are stranded in the pre-identity namespace.

    Reported rather than merely fixable: a deployment that switched modes has no
    other signal that its old memories exist, because every read simply comes
    back empty."""
    require_superadmin(p)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory WHERE namespace=%s",
                    (ORPHAN_NAMESPACE,))
        return {"orphans": cur.fetchone()[0]}


def adopt_orphans(conn, p: Principal, *, namespace_id: str, vectors=None) -> dict:
    """Move every stranded memory into a real namespace.

    Idempotent: with nothing stranded it changes nothing and says so, so it is
    safe to call twice or to leave wired into a startup script.

    The order matters and is the whole subtlety. A memory's namespace is written
    down twice — on the row, and on every chunk vector — and the two live in
    different stores when the backend is Qdrant, which cannot join a Postgres
    transaction. Rewriting the rows first and dying before the vectors leaves
    lexical recall working and semantic recall returning NOTHING for the adopted
    memories: not an error, just an empty answer, which is the failure this
    codebase treats as worse than a crash. So the vectors move first. A run that
    dies in between leaves chunks pointing at a namespace whose memories are
    still stranded — and stranded memories were already unreachable, so the
    halfway state answers nothing wrong, and running it again finishes the job.
    """
    require_superadmin(p)
    require_namespace_admin(conn, p, namespace_id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory WHERE namespace=%s",
                    (ORPHAN_NAMESPACE,))
        found = cur.fetchone()[0]
    if not found:
        return {"adopted": 0, "chunks": 0, "namespace_id": namespace_id}

    chunks = 0
    if vectors is not None:
        chunks = vectors.retag_namespace(conn, ORPHAN_NAMESPACE, namespace_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE memory SET namespace=%s WHERE namespace=%s",
                    (namespace_id, ORPHAN_NAMESPACE))
        adopted = cur.rowcount
    return {"adopted": adopted, "chunks": chunks, "namespace_id": namespace_id}


def request_access(conn, p: Principal, *, namespace_id: str,
                   permission: str = "read") -> dict:
    """Ask for membership. Any authenticated user with an owning account may.

    The answer says what happened to the *request*, never what exists: a
    namespace the caller cannot reach and a namespace that does not exist are
    reported identically, because telling them apart is exactly what an outsider
    probing uuids would be after. It carries no request id for the same reason —
    and the requester has no use for one, since deciding a request belongs to
    whoever administers the namespace, who reads ids from `list_requests`.

    Already reaching it is reported plainly: that is the caller's own access,
    which `list_spaces` shows them anyway.
    """
    if p.user_id is None:
        raise identity.AuthError("this token has no owning user")
    perm = identity.reaches(conn, p.user_id, namespace_id)
    if perm is not None:
        return {"status": "already_reachable", "permission": perm}
    identity.request_access(conn, p.user_id, namespace_id, permission)
    return {"status": "submitted"}


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
