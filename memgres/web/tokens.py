"""Tokens a person issues for their own AI clients, from the panel.

Narrower than the MCP self-service tool on purpose:

* **no ``admin`` ceiling.** An admin-ceiling token can mint and revoke tokens;
  a browser click should not hand an agent that. Such tokens stay a CLI/admin act.
* **an expiry is mandatory.** A token made in a browser is copied into a config
  file on some laptop, which is exactly the credential nobody remembers to
  rotate.
* **only namespaces the account reaches.** Scoping is not granting, but a token
  pinned to a space its owner cannot see is a support ticket, not a credential.
* **a cap on live tokens per account**, so a scripted session cannot fill the
  table.

The secret is returned once, in the response to the person who asked for it —
not to a file. ``MEMGRES_TOKEN_SINK`` exists to keep secrets out of *agent*
transcripts; here the reader is a human looking at a dialog that says "copy it
now".
"""

import datetime as dt
from typing import Optional

from .. import admin, identity

# The panel's menu. The policy itself lives in `admin` (SELF_*) so every door
# obeys it; what stays here is the narrower CHOICE a browser offers — four
# expiries rather than any number of days up to the ceiling.
EXPIRY_CHOICES = (30, 90, 180, 365)
PERMISSIONS = admin.SELF_PERMISSIONS
MAX_LABEL = admin.SELF_MAX_LABEL
MAX_LIVE_TOKENS = admin.SELF_MAX_LIVE_TOKENS


class TokenRefused(ValueError):
    pass


def list_own(conn, user_id: str) -> list:
    rows = identity.list_tokens(conn, user_id)
    ns_ids = sorted({r["namespace_id"] for r in rows if r["namespace_id"]})
    names = {}
    if ns_ids:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM namespace WHERE id = ANY(%s::uuid[])", (ns_ids,))
            names = {str(i): n for i, n in cur.fetchall()}
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for r in reversed(rows):                      # newest first
        expired = r["expires_at"] is not None and r["expires_at"] <= now
        out.append({
            "id": r["id"],
            "label": r["label"],
            "permission": r["permission"],
            "namespace_id": r["namespace_id"],
            "namespace": names.get(r["namespace_id"]) if r["namespace_id"] else None,
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
            "expires_at": r["expires_at"],
            "revoked_at": r["revoked_at"],
            "state": "revoked" if r["revoked_at"] else "expired" if expired else "active",
        })
    return out


def issue_own(conn, principal, *, label: str, permission: str,
              namespace_id: Optional[str], expires_days: int) -> dict:
    """A token for the person's own clients, through the shared self-service
    tier — so the cap, the mandatory expiry and the refusal of an admin ceiling
    are the deployment's rules rather than this page's."""
    try:
        out = admin.issue_own_token(
            conn, principal, label=label, permission=permission,
            namespace_id=namespace_id, expires_days=expires_days,
            allowed_expiries=EXPIRY_CHOICES, defer_delivery=True)
    except admin.Refused as e:
        raise TokenRefused(str(e)) from None
    except admin.Forbidden as e:
        raise TokenRefused(str(e)) from None
    return {"id": out["id"], "token": out.pop("secret"),
            "permission": out["permission"], "namespace_id": out["namespace_id"],
            "expires_at": out["expires_at"]}


def revoke_own(conn, user_id: str, token_id: str) -> bool:
    """Revoke one of the caller's tokens. Someone else's token, or no token at
    all, answers the same: not found."""
    try:
        token_id = identity._as_uuid(token_id)
    except ValueError:
        return False
    if identity.token_owner(conn, token_id) != user_id:
        return False
    identity.revoke_token(conn, token_id)
    return True


def mount(app, cfg, pool, panel) -> None:
    from fastapi import Body, HTTPException, Request

    from .sessions import control_principal

    def _own_account(s):
        if s.user_id is None:
            raise HTTPException(409, "the administrator token has no account to hold tokens")
        return s.user_id

    @app.get("/ui/api/tokens")
    def my_tokens(request: Request):
        uid = _own_account(panel["session"](request))
        with pool.connection() as conn:
            return {"tokens": list_own(conn, uid), "expiry_choices": list(EXPIRY_CHOICES),
                    "permissions": list(PERMISSIONS), "mcp_url": cfg.mcp_public_url or None}

    @app.post("/ui/api/tokens", status_code=201)
    def new_token(request: Request, label: str = Body("", embed=True),
                  permission: str = Body("write", embed=True),
                  namespace_id: Optional[str] = Body(None, embed=True),
                  expires_days: int = Body(90, embed=True)):
        sess = panel["changing"](request)
        _own_account(sess)
        with pool.connection() as conn, conn.transaction():
            try:
                # the CONTROL principal: minting a token is a control-plane act,
                # and the session's read ceiling is for reading memory
                return issue_own(conn, control_principal(sess), label=label,
                                 permission=permission,
                                 namespace_id=namespace_id, expires_days=expires_days)
            except TokenRefused as e:
                raise HTTPException(422, str(e))

    @app.delete("/ui/api/tokens/{token_id}")
    def revoke(token_id: str, request: Request):
        uid = _own_account(panel["changing"](request))
        with pool.connection() as conn, conn.transaction():
            if not revoke_own(conn, uid, token_id):
                raise HTTPException(404, "not found")
        return {"revoked": token_id}
