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

    It still needs an admin-CEILING token. The superadmin branch used to return
    before any ceiling check, so a deliberately weakened credential — a
    *read-only* superadmin token, the shape the docs recommend handing an agent
    — could rewrite any namespace's `instruction` (the routing hint other agents
    read to decide where memories land) and approve access requests, granting
    strangers write membership anywhere. Neither is a read. The check for
    everyone else was already there, in `perm_min(membership, ceiling)`; the
    role was skipping past it.
    """
    # A scoped token is pinned to one namespace, and administering a DIFFERENT
    # one is outside what it was issued for — including for a superadmin, whose
    # role would otherwise make the pin meaningless.
    if (p.scope_namespace_id is not None
            and str(p.scope_namespace_id) != str(namespace_id)):
        raise Forbidden("this token is scoped to a different namespace")
    if p.is_admin:
        if p.permission != "admin":
            raise Forbidden(
                f"administering a namespace requires an admin-ceiling token "
                f"(this one grants {p.permission})")
        # A superadmin skips the membership lookup, and with it the only thing
        # that used to establish the namespace EXISTS. Since access requests are
        # now recorded whether or not their namespace does, that left the one
        # caller who could see an orphaned request — and approving it failed on
        # `namespace_member`'s foreign key, i.e. a raw driver error rather than a
        # refusal, with the safety of the whole arrangement resting on a
        # constraint in a different table. Checked here because this is the
        # single point every per-namespace admin action funnels through.
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM namespace WHERE id=%s",
                        (identity._as_uuid(namespace_id),))
            if cur.fetchone() is None:
                raise identity.SpaceNotFound("no such namespace")
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
        # Mirrors every condition `create_own_namespace` enforces, not just the
        # right itself: a read-only or scoped token, and a credential with no
        # owning user, are all refused there, and saying otherwise here would
        # advertise a door that always closes.
        "can_create_namespace": (
            (p.user_id is not None or p.provisional)
            and p.scope_namespace_id is None
            and identity.perm_at_least(p.permission, "write")
            and identity.can_create_namespace(conn, p)),
        "can_write": identity.perm_at_least(p.permission, "write"),
        # The env break-glass root owns no account, so it has no tokens to
        # manage however unscoped and admin-ceiling it is.
        "can_manage_own_tokens": _has_full_credential(p) and p.user_id is not None,
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
                   instruction: Optional[str] = None,
                   name: Optional[str] = None) -> dict:
    """Amend a namespace's description or routing instruction.

    `create_namespace` is an idempotent upsert that ignores conflicts, so
    re-creating with corrected text silently does nothing — until now a typo in
    the instruction agents read to choose a namespace could not be fixed through
    any door at all.
    """
    require_namespace_admin(conn, p, namespace_id)
    identity.edit_namespace(conn, namespace_id, description=description,
                            instruction=instruction, name=name)
    out = {"namespace_id": namespace_id}
    if name is not None:
        out["name"] = name
        out["warning"] = ("renamed — anyone addressing it by the old name now "
                          "gets 'no such namespace'. Ids and aliases still work")
    return out


def require_namespace_owner(conn, p: Principal, namespace_id: str) -> None:
    """Owner-or-superadmin, a tier narrower than `require_namespace_admin`.

    For the acts that dispose of the namespace itself rather than work inside
    it. An admin MEMBER was given authority over the contents; giving away
    somebody else's namespace is not part of that, and the difference only
    matters here — which is why this is its own check rather than a flag.
    """
    require_namespace_admin(conn, p, namespace_id)      # ceiling + scope + exists
    if p.is_admin:
        return
    if identity.namespace_owner(conn, namespace_id) != p.user_id:
        raise Forbidden("only the owner (or a superadmin) may do this")


def add_member(conn, p: Principal, *, namespace_id: str, user_id: str,
               permission: str = "read") -> dict:
    """Share a namespace with another user.

    Authorized per-NAMESPACE, not deployment-wide. It used to demand superadmin,
    on the reasoning that sharing reaches across tenants — but the thing being
    shared is the caller's OWN namespace, and requiring the deployment's root
    for that made "let a colleague into my cabinet" an operator ticket. What it
    still demands is an admin-ceiling credential for that namespace, so a
    read-only or differently-scoped token cannot hand out access.
    """
    require_namespace_admin(conn, p, namespace_id)
    identity.add_member(conn, namespace_id, user_id, permission)
    return {"namespace_id": namespace_id, "user_id": user_id,
            "permission": permission}


def remove_member(conn, p: Principal, *, namespace_id: str, user_id: str) -> dict:
    """Un-share a namespace. `removed: false` means they were not a member.

    The other half of `add_member`, and its absence was the sharpest gap in this
    control plane: access could be granted and never taken back except by
    revoking every token the person held, which cuts them off from everything
    rather than from this.
    """
    require_namespace_admin(conn, p, namespace_id)
    return {"namespace_id": namespace_id, "user_id": user_id,
            "removed": identity.remove_member(conn, namespace_id, user_id)}


def transfer_namespace(conn, p: Principal, *, namespace_id: str,
                       new_owner_user_id: str,
                       keep_previous_owner: Optional[str] = "admin") -> dict:
    """Hand a namespace to another account — owner or superadmin.

    The outgoing owner stays behind as an `admin` member unless
    `keep_previous_owner` is null. Defaulting to keeping them is the safer
    footing: the alternative is a single call that removes the caller from a
    namespace whose contents they may be the only one who knows.
    """
    require_namespace_owner(conn, p, namespace_id)
    return identity.transfer_namespace(
        conn, namespace_id, new_owner_user_id,
        keep_previous_owner=keep_previous_owner)


def set_disabled(conn, p: Principal, *, user_id: str, disabled: bool) -> dict:
    """Switch an account off, or back on. Every token it holds stops at once.

    Offboarding as ONE act. Doing it by revoking tokens one at a time is a loop
    that has to be complete to be correct, and nothing stops a new token being
    issued afterwards. Reversible and destructive of nothing — authorship,
    namespaces and memberships all survive, which is what makes it usable for
    "gone for now" as well as "gone".
    """
    require_manage_users(p)
    _require_target_is_plain_user(conn, p, user_id,
                                  "disabling an account" if disabled
                                  else "re-enabling an account")
    return identity.set_disabled(conn, user_id, disabled)


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

def _unreachable_warning(conn, user_id: str,
                         namespace_id: Optional[str]) -> Optional[str]:
    """Warn when a credential is about to be scoped to a namespace its owner
    cannot reach.

    Scoping is not granting: reach comes from ownership or membership, and a
    token pinned to a namespace the account is not in is perfectly valid and
    reaches nothing. The person enrols, everything answers "no namespace", and
    it reads as a broken server rather than an unfinished provisioning. A
    warning rather than a refusal, because issuing the credential first and
    adding the membership after is a legitimate order.
    """
    if not namespace_id:
        return None
    if identity.reaches(conn, user_id, namespace_id) is not None:
        return None
    return ("that user cannot reach this namespace yet, so the token will be "
            "valid and see NOTHING — add them with memory_admin_add_member "
            "(or hand the namespace over) before they try to use it")


def issue_token(conn, p: Principal, *, user_id: str,
                namespace_id: Optional[str] = None, permission: str = "write",
                label: str = "", expires_days: Optional[int] = None,
                sink_dir: str = "") -> dict:
    """Mint a token for `user_id`. The secret is returned once and never again.

    `expires_days` rather than a timestamp: both doors were converting the same
    way, so the conversion belongs here.

    `sink_dir` (the deployment's `MEMGRES_TOKEN_SINK`) diverts the secret to a
    0600 file on the server and returns its path instead — see
    `identity.stash_secret`. The caller then never holds the secret at all,
    which is the point when the caller is an agent.
    """
    require_manage_users(p)
    _require_target_is_plain_user(conn, p, user_id, "issuing a token")
    expires_at = None
    if expires_days:
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expires_days)
    warning = _unreachable_warning(conn, user_id, namespace_id)
    secret, tid = identity.issue_token(conn, user_id, namespace_id=namespace_id,
                                       permission=permission, label=label,
                                       expires_at=expires_at)
    out = deliver_secret(secret, tid, sink_dir)
    if warning:
        out["warning"] = warning
    return out


def deliver_secret(secret: str, token_id: str, sink_dir: str) -> dict:
    """The reply for a freshly minted token: the secret itself, or — when the
    deployment set a sink — only where it was put. One function so both minting
    doors (this module and the MCP self-service tool) cannot disagree about
    whether a secret is allowed into a response body."""
    if sink_dir:
        path = identity.stash_secret(sink_dir, token_id, secret)
        return {"id": token_id, "delivered": "file", "path": path,
                "exposed": False,
                "note": "the secret was written to that file on the server and "
                        "deliberately NOT returned here — read it there"}
    return {"token": secret, "id": token_id, "exposed": True,
            "note": "store this now — it is not recoverable. This secret was "
                    "returned in a reply: if that reply reached an LLM, treat "
                    "the token as exposed and rotate it once delivered. Set "
                    "MEMGRES_TOKEN_SINK to stop returning secrets at all."}


def create_enrollment(conn, p: Principal, *, user_id: str,
                      namespace_id: Optional[str] = None,
                      permission: str = "write", label: str = "",
                      expires_minutes: Optional[int] = None) -> dict:
    """Mint a one-time key that lets `user_id` bind a token THEY generate.

    Provisioning-tier, and gated exactly like `issue_token`, because it grants
    the same thing by another route: whoever redeems it ends up holding a
    credential for that account. What it does not do is put a secret anywhere —
    the key is worthless after one use and after `expires_minutes`, and the
    token itself is created on the far side and never travels.
    """
    require_manage_users(p)
    _require_target_is_plain_user(conn, p, user_id, "issuing an enrollment key")
    kw = {} if expires_minutes is None else {"expires_minutes": expires_minutes}
    out = identity.create_enrollment(
        conn, user_id, namespace_id=namespace_id, permission=permission,
        label=label, created_by=p.user_id, **kw)
    out["note"] = ("give this key to its owner over any channel you would use "
                   "for a meeting link — it is single-use and short-lived. They "
                   "generate their own token, put it in their client's config, "
                   "and call memory_enroll with this key.")
    warning = _unreachable_warning(conn, user_id, namespace_id)
    if warning:
        out["warning"] = warning
    return out


def list_enrollments(conn, p: Principal, *,
                     user_id: Optional[str] = None) -> List[dict]:
    """Enrollment keys and what became of them — metadata only, never a key."""
    require_manage_users(p)
    if user_id is not None:
        _require_target_is_plain_user(conn, p, user_id, "listing enrollment keys")
    return identity.list_enrollments(conn, user_id=user_id)


def revoke_enrollment(conn, p: Principal, *, enrollment_id: str) -> bool:
    """Kill an unredeemed key. False if it was already spent, revoked or absent."""
    require_manage_users(p)
    _require_target_is_plain_user(conn, p,
                                  identity.enrollment_owner(conn, enrollment_id),
                                  "revoking an enrollment key")
    return identity.revoke_enrollment(conn, enrollment_id)


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

    The two indistinguishable cases run the same code — one reachability query,
    one upsert — because answers that match while the work differs leave the
    difference in the response TIME. See `identity.request_access`.
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
