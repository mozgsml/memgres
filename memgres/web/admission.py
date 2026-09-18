"""Who a provider's word lets in — and what happens to everyone else.

Authentication is not admission. A provider says who someone is; this module
decides whether memgres lets them in, in this order:

1. the provider's ``require`` conditions and ``allowed_email_domains`` —
   failing either refuses, and creates nothing;
2. an existing link (issuer, subject) → that account, unless it is disabled;
3. a verified email equal to an account's email → ``on_email_match``;
4. otherwise → ``on_no_match``.

Invariants:

* identity is (issuer, subject). Email is only how a FIRST link can be found,
  and only when the provider says it is verified.
* an email match never links an **administrator** account on its own: that
  would make "has a mailbox with the admin's address at some provider" the same
  as "is the admin". It becomes a request for a human to decide.
* ``pending`` creates no account — only a request an administrator decides.
* a person rejected within the last 30 days is refused without a new request,
  so rejection does not turn into a queue someone can refill at will.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from .. import identity
from ..identity import ADMIN_ROLES
from .oidc_config import Provider

MAX_PENDING = 500            # open requests across the deployment
MAX_PENDING_PER_PROVIDER = 100   # so one open provider cannot crowd out the rest
REJECTION_MEMORY_DAYS = 30


@dataclass
class Outcome:
    kind: str                           # signed_in | linked | pending | denied
    user_id: Optional[str] = None
    reason: str = ""                    # a short code the panel turns into words
    identity_id: Optional[str] = None   # the sign-in method used, for the session


def verified_email(claims: dict) -> Optional[str]:
    email = claims.get("email")
    verified = claims.get("email_verified")
    if isinstance(verified, str):
        verified = verified.lower() == "true"
    if isinstance(email, str) and "@" in email and verified is True:
        return email.strip()
    return None


def _display_name(claims: dict) -> str:
    for key in ("name", "preferred_username", "nickname"):
        v = claims.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
    email = claims.get("email")
    return email.split("@")[0][:200] if isinstance(email, str) and "@" in email else ""


def passes_rules(provider: Provider, claims: dict) -> bool:
    if not all(c.holds(claims) for c in provider.require):
        return False
    if provider.allowed_email_domains:
        email = verified_email(claims)
        if not email or email.rsplit("@", 1)[1].lower() not in provider.allowed_email_domains:
            return False
    return True


def _link(cur, user_id: str, provider: Provider, sub: str, email: Optional[str]) -> str:
    cur.execute(
        "INSERT INTO app_user_identity (issuer, subject, user_id, provider, email, last_login_at) "
        "VALUES (%s, %s, %s, %s, %s, now()) RETURNING id", (provider.issuer, sub, user_id, provider.id, email))
    return str(cur.fetchone()[0])


def _request(cur, provider: Provider, claims: dict, suggested: Optional[str]) -> Outcome:
    sub = claims["sub"]
    cur.execute("UPDATE signin_request SET last_seen_at = now(), suggested_user_id = %s "
                "WHERE issuer = %s AND subject = %s AND status = 'pending' RETURNING id",
                (suggested, provider.issuer, sub))
    if cur.fetchone():
        return Outcome("pending")
    if verified_email(claims) is None:
        # A request an administrator must judge needs at least an address the
        # provider vouches for; without one, anyone who can make accounts at an
        # open provider could fill the queue with nobodies.
        return Outcome("denied", reason="no_account")
    cur.execute("DELETE FROM signin_request WHERE status <> 'pending' "
                "AND decided_at < now() - make_interval(days => %s)", (REJECTION_MEMORY_DAYS * 3,))
    cur.execute("SELECT count(*), count(*) FILTER (WHERE provider = %s) FROM signin_request "
                "WHERE status = 'pending'", (provider.id,))
    total, mine = cur.fetchone()
    if total >= MAX_PENDING or mine >= MAX_PENDING_PER_PROVIDER:
        return Outcome("denied", reason="queue_full")
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    cur.execute(
        "INSERT INTO signin_request (provider, issuer, subject, email, email_verified, name, "
        "suggested_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (provider.id, provider.issuer, sub, email, verified_email(claims) is not None,
         _display_name(claims), suggested))
    return Outcome("pending")


def _cut_off(conn, cfg, user_id: str) -> None:
    """A linked person no longer meets the provider's rules: end their sessions
    and revoke their tokens. The bootstrap admin token is spared — it is the
    operator's way back in, and revoking it would lock the deployment."""
    keep = identity.token_hash(cfg.admin_token) if cfg.admin_token else ""
    with conn.cursor() as cur:
        cur.execute("UPDATE web_session SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                    (user_id,))
        # the seeded token carries label 'bootstrap' whether it came from the
        # env or from MEMGRES_ADMIN_TOKEN_FILE (where cfg.admin_token is empty)
        cur.execute("UPDATE token SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL "
                    "AND token_hash <> %s AND label <> 'bootstrap'", (user_id, keep))


def admit(conn, cfg, provider: Provider, claims: dict, *, link_user_id: Optional[str] = None) -> Outcome:
    """Decide one completed sign-in. Runs inside the caller's transaction."""
    sub = claims["sub"]
    email = verified_email(claims)
    with conn.cursor() as cur:
        cur.execute("SELECT i.user_id, (u.disabled_at IS NOT NULL) FROM app_user_identity i "
                    "JOIN app_user u ON u.id = i.user_id WHERE i.issuer = %s AND i.subject = %s",
                    (provider.issuer, sub))
        row = cur.fetchone()
        linked_to = str(row[0]) if row else None
        linked_disabled = bool(row[1]) if row else False

        if not passes_rules(provider, claims):
            if linked_to and provider.sync_on_login:
                _cut_off(conn, cfg, linked_to)
            return Outcome("denied", reason="rules")

        # ─── adding a method to the signed-in account ───────────────────────
        if link_user_id:
            if linked_to == link_user_id:
                return Outcome("linked", user_id=link_user_id)
            if linked_to:
                return Outcome("denied", reason="taken")
            # one link at a time per account: two callbacks racing must not both
            # pass the same-provider check below
            cur.execute("SELECT (disabled_at IS NOT NULL) FROM app_user WHERE id = %s FOR UPDATE",
                        (link_user_id,))
            acct = cur.fetchone()
            if acct is None or acct[0]:
                return Outcome("denied", reason="disabled")
            cur.execute("SELECT 1 FROM app_user_identity WHERE user_id = %s AND issuer = %s",
                        (link_user_id, provider.issuer))
            if cur.fetchone():
                # a second account at the same provider is not the same person
                # by default; an administrator can link it deliberately
                return Outcome("denied", reason="same_provider")
            _link(cur, link_user_id, provider, sub, email)
            return Outcome("linked", user_id=link_user_id)

        # ─── an existing link ───────────────────────────────────────────────
        if linked_to:
            if linked_disabled:
                return Outcome("denied", reason="disabled")
            cur.execute("UPDATE app_user_identity SET last_login_at = now(), email = COALESCE(%s, email) "
                        "WHERE issuer = %s AND subject = %s RETURNING id", (email, provider.issuer, sub))
            return Outcome("signed_in", user_id=linked_to, identity_id=str(cur.fetchone()[0]))

        cur.execute("SELECT 1 FROM signin_request WHERE issuer = %s AND subject = %s "
                    "AND status = 'rejected' AND decided_at > now() - make_interval(days => %s)",
                    (provider.issuer, sub, REJECTION_MEMORY_DAYS))
        if cur.fetchone():
            return Outcome("denied", reason="rejected")

        # ─── a verified email that matches an account ───────────────────────
        match = None
        if email:
            cur.execute("SELECT id, role, (disabled_at IS NOT NULL) FROM app_user "
                        "WHERE email IS NOT NULL AND lower(email) = lower(%s)", (email,))
            match = cur.fetchone()
        if match:
            uid, role, disabled = str(match[0]), match[1], bool(match[2])
            action = provider.on_email_match
            if action == "deny" or disabled:
                return Outcome("denied", reason="disabled" if disabled else "no_account")
            cur.execute("SELECT 1 FROM app_user_identity WHERE user_id = %s AND issuer = %s", (uid, provider.issuer))
            same_provider = cur.fetchone() is not None
            if action == "link" and role not in ADMIN_ROLES and not same_provider:
                iid = _link(cur, uid, provider, sub, email)
                return Outcome("signed_in", user_id=uid, identity_id=iid)
            return _request(cur, provider, claims, uid)

        # ─── nobody ─────────────────────────────────────────────────────────
        if provider.on_no_match == "deny":
            return Outcome("denied", reason="no_account")
        if provider.on_no_match == "pending":
            return _request(cur, provider, claims, None)
        uid = create_account(conn, claims)
        iid = _link(cur, uid, provider, sub, email)
        return Outcome("signed_in", user_id=uid, identity_id=iid)


def create_account(conn, claims_or_request: dict) -> str:
    """A plain user with no spaces — access to memory is still granted by a
    person. The email is recorded only when verified and not already in use."""
    email = claims_or_request.get("verified_email", verified_email(claims_or_request))
    name = claims_or_request.get("display_name") or _display_name(claims_or_request)
    with conn.cursor() as cur:
        if email:
            cur.execute("SELECT 1 FROM app_user WHERE lower(email) = lower(%s)", (email,))
            if cur.fetchone():
                email = None
    profile = {"full_name": name}
    if email:
        profile["email"] = email
    return identity.create_user(conn, name=(email or name or "user")[:200], **profile)


# ─── the administrator's side ────────────────────────────────────────────────
def list_requests(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.id, r.provider, r.email, r.email_verified, r.name, r.created_at, r.last_seen_at, "
            "       u.id, u.name, u.full_name, u.email, u.role "
            "FROM signin_request r LEFT JOIN app_user u ON u.id = r.suggested_user_id "
            "WHERE r.status = 'pending' ORDER BY r.created_at")
        out = []
        for (rid, prov, email, verified, name, created, seen, uid, uname, ufull, uemail, urole) in cur.fetchall():
            out.append({"id": str(rid), "provider": prov, "email": email, "email_verified": verified,
                        "name": name, "created_at": created, "last_seen_at": seen,
                        "suggested": {"id": str(uid), "name": uname, "full_name": ufull, "email": uemail,
                                      "role": urole} if uid else None})
        return out


class DecisionRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def decide(conn, *, actor_role: str, actor_user_id: Optional[str], request_id: str,
           action: str, user_id: Optional[str] = None) -> dict:
    """link (to `user_id`, or the suggested account) | create | reject.

    A user manager cannot link a sign-in to an administrator's account: that
    would let the middle tier hand someone admin authority, which it may not do
    any other way either. Only a superadmin (or the env root) may."""
    if actor_role not in ADMIN_ROLES:
        raise DecisionRefused(403, "deciding sign-in requests needs user_manager or superadmin")
    if action not in ("link", "create", "reject"):
        raise DecisionRefused(422, "action must be link, create or reject")
    try:
        request_id = identity._as_uuid(request_id)
    except ValueError:
        raise DecisionRefused(404, "not found") from None
    with conn.cursor() as cur:
        cur.execute("SELECT provider, issuer, subject, email, email_verified, name, suggested_user_id "
                    "FROM signin_request WHERE id = %s AND status = 'pending' FOR UPDATE", (request_id,))
        row = cur.fetchone()
        if row is None:
            raise DecisionRefused(404, "not found")
        prov, issuer, sub, email, verified, name, suggested = row
        if action == "reject":
            cur.execute("UPDATE signin_request SET status = 'rejected', decided_at = now(), decided_by = %s "
                        "WHERE id = %s", (actor_user_id, request_id))
            return {"id": request_id, "status": "rejected"}

        cur.execute("SELECT user_id FROM app_user_identity WHERE issuer = %s AND subject = %s", (issuer, sub))
        if cur.fetchone():
            raise DecisionRefused(409, "this sign-in is already linked to an account")

        if action == "link":
            target = user_id or (str(suggested) if suggested else None)
            if not target:
                raise DecisionRefused(422, "choose the account to link to")
            try:
                target = identity._as_uuid(target)
            except ValueError:
                raise DecisionRefused(404, "no such account") from None
            cur.execute("SELECT role, (disabled_at IS NOT NULL) FROM app_user WHERE id = %s", (target,))
            acct = cur.fetchone()
            if acct is None:
                raise DecisionRefused(404, "no such account")
            if acct[1]:
                raise DecisionRefused(409, "that account is disabled")
            if acct[0] in ADMIN_ROLES and actor_role != "superadmin":
                raise DecisionRefused(403, "only a superadmin can link a sign-in to an administrator account")
        else:
            target = create_account(conn, {"verified_email": email if verified else None,
                                           "display_name": name})
        cur.execute("INSERT INTO app_user_identity (issuer, subject, user_id, provider, email) "
                    "VALUES (%s, %s, %s, %s, %s)", (issuer, sub, target, prov, email if verified else None))
        cur.execute("UPDATE signin_request SET status = 'approved', decided_at = now(), decided_by = %s, "
                    "user_id = %s WHERE id = %s", (actor_user_id, target, request_id))
    return {"id": request_id, "status": "approved", "user_id": target}


def identities(conn, user_id: str, providers: dict) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT id, provider, email, linked_at, last_login_at FROM app_user_identity "
                    "WHERE user_id = %s ORDER BY linked_at", (user_id,))
        return [{"id": str(i), "provider": p, "label": providers[p].label if p in providers else p,
                 "email": e, "linked_at": la, "last_login_at": ll} for i, p, e, la, ll in cur.fetchall()]


def unlink_own(conn, user_id: str, identity_id: str) -> str:
    """Remove one of your sign-in methods. Not the last one: without it you
    could not come back, and only an administrator can fix that."""
    try:
        identity_id = identity._as_uuid(identity_id)
    except ValueError:
        return "not_found"
    with conn.cursor() as cur:
        cur.execute("SELECT id, provider FROM app_user_identity WHERE user_id = %s FOR UPDATE", (user_id,))
        rows = {str(i): p for i, p in cur.fetchall()}
        if identity_id not in rows:
            return "not_found"
        if len(rows) == 1:
            return "last"
        cur.execute("DELETE FROM app_user_identity WHERE id = %s", (identity_id,))
        cur.execute("UPDATE web_session SET revoked_at = now() WHERE user_id = %s AND identity_id = %s "
                    "AND revoked_at IS NULL", (user_id, identity_id))
    return "unlinked"
