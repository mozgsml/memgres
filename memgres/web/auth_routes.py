"""OIDC sign-in over HTTP: start, callback, a person's sign-in methods, and the
administrator's queue of people waiting to be let in."""

import hashlib
import logging
import secrets

import psycopg

from .. import identity
from ..identity import ADMIN_ROLES
from . import admission, sessions, spaces
from .oidc import Client, OIDCError, merged_claims, new_pkce

log = logging.getLogger("memgres.web")

FLOW_MINUTES = 10
MAX_OPEN_FLOWS = 10_000
STARTS_PER_CLIENT = 30        # sign-ins one client may start per FLOW_MINUTES


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mount(app, cfg, pool, panel, providers, fetch=None) -> None:
    from fastapi import Body, HTTPException, Request
    from fastapi.responses import RedirectResponse

    clients = {pid: Client(p, fetch=fetch) for pid, p in providers.items()}
    flow_cookie = "__Host-memgres_oidc" if cfg.web_cookie_secure else "memgres_oidc"

    def _base(request: Request) -> str:
        return cfg.public_url or str(request.base_url).rstrip("/")

    def _redirect_uri(request: Request, pid: str) -> str:
        return f"{_base(request)}/ui/auth/{pid}/callback"

    def _go(path: str, *, clear_flow: bool = True):
        r = RedirectResponse(path, status_code=303)
        if clear_flow:
            r.delete_cookie(flow_cookie, path="/", secure=cfg.web_cookie_secure, httponly=True, samesite="lax")
        return r

    from .routes import _Throttle
    starts = _Throttle(limit=STARTS_PER_CLIENT, window_s=FLOW_MINUTES * 60)

    def _begin(request: Request, pid: str, *, link_session=None) -> tuple:
        """Record a flow and return (authorization URL, browser secret). Raises
        OIDCError when the provider cannot be reached."""
        client = clients.get(pid)
        if client is None:
            raise HTTPException(404, "no such sign-in provider")
        key = panel["client_key"](request)
        if starts.blocked(key):
            raise HTTPException(429, "too many sign-ins started — wait a few minutes")
        starts.fail(key)
        state, nonce, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, challenge = new_pkce()
        url = client.authorize_url(redirect_uri=_redirect_uri(request, pid), state=state,
                                   nonce=nonce, challenge=challenge)
        with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM oidc_flow WHERE expires_at < now()")
            cur.execute("SELECT count(*) FROM oidc_flow")
            if cur.fetchone()[0] >= MAX_OPEN_FLOWS:
                raise HTTPException(503, "too many sign-ins in progress — try again in a few minutes")
            cur.execute(
                "INSERT INTO oidc_flow (state_hash, browser_hash, provider, nonce, code_verifier, link_user_id, "
                "link_session_hash, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(mins => %s))",
                (_h(state), _h(browser), pid, nonce, verifier,
                 link_session.user_id if link_session else None,
                 link_session.id_hash if link_session else None, FLOW_MINUTES))
        return url, browser

    def _flow_cookie(response, browser: str) -> None:
        response.set_cookie(flow_cookie, browser, max_age=FLOW_MINUTES * 60, path="/",
                            secure=cfg.web_cookie_secure, httponly=True, samesite="lax")

    # ─── start ──────────────────────────────────────────────────────────────
    @app.get("/ui/auth/{pid}/start", include_in_schema=False)
    def start(pid: str, request: Request):
        """Signing in. Needs nothing: whatever happens next, the person ends up
        signed in as the provider says they are, or not at all."""
        try:
            url, browser = _begin(request, pid)
        except OIDCError as e:
            log.warning("oidc start failed: %s", e)
            return _go("/signin?auth=provider_down")
        r = RedirectResponse(url, status_code=302)
        _flow_cookie(r, browser)
        return r

    @app.post("/ui/api/me/signins/link")
    def start_link(request: Request, provider: str = Body(..., embed=True)):
        """Adding a method to the signed-in account. A state change, so it goes
        through the CSRF-checked door rather than a link any page could follow."""
        s = panel["changing"](request)
        if s.user_id is None:
            raise HTTPException(409, "the administrator token has no account")
        from fastapi.responses import JSONResponse
        try:
            url, browser = _begin(request, provider, link_session=s)
        except OIDCError as e:
            log.warning("oidc link start failed: %s", e)
            raise HTTPException(502, "the sign-in provider is not answering")
        r = JSONResponse({"url": url})
        _flow_cookie(r, browser)
        return r

    # ─── callback ───────────────────────────────────────────────────────────
    @app.get("/ui/auth/{pid}/callback", include_in_schema=False)
    def callback(pid: str, request: Request):
        q = request.query_params
        client = clients.get(pid)
        state = q.get("state") or ""
        browser = request.cookies.get(flow_cookie) or ""
        if client is None or not state or len(state) > 256:
            return _go("/signin?auth=expired")
        with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            # single use: the row is consumed whatever happens next
            cur.execute("DELETE FROM oidc_flow WHERE state_hash = %s RETURNING browser_hash, provider, nonce, "
                        "code_verifier, link_user_id, expires_at > now(), link_session_hash", (_h(state),))
            flow = cur.fetchone()
        if flow is None:
            return _go("/signin?auth=expired")
        browser_hash, flow_pid, nonce, verifier, link_user, alive, link_session_hash = flow
        link_user = str(link_user) if link_user else None
        back = "/account/signins" if link_user else "/signin"
        if not alive or flow_pid != pid or not browser or not secrets.compare_digest(browser_hash, _h(browser)):
            return _go(f"{back}?auth=expired")
        if q.get("error"):
            return _go(f"{back}?auth=cancelled")
        code = q.get("code")
        if not code or len(code) > 4096:
            return _go(f"{back}?auth=failed")
        try:
            tokens = client.exchange(code=code, redirect_uri=_redirect_uri(request, pid), verifier=verifier)
            id_claims = client.verify_id_token(tokens["id_token"], nonce=nonce)
            claims = merged_claims(id_claims, client.userinfo(tokens.get("access_token"), id_claims["sub"]))
        except OIDCError as e:
            log.warning("oidc callback rejected: %s", e)
            return _go(f"{back}?auth=failed")

        provider = providers[pid]
        if link_user:
            # the session that asked for this link must still stand: signing out,
            # a revoked token or a disabled account in the meantime stops it
            with pool.connection() as conn:
                starter = sessions.load_by_hash(conn, cfg, link_session_hash)
            if starter is None or starter.user_id != link_user:
                return _go(f"{back}?auth=expired")
        try:
            with pool.connection() as conn, conn.transaction():
                outcome = admission.admit(conn, cfg, provider, claims, link_user_id=link_user)
                sid = None
                if outcome.kind in ("signed_in", "linked"):
                    # let in: open invitations for the address the provider vouches for apply now
                    spaces.apply_invites(conn, outcome.user_id, admission.verified_email(claims))
                if outcome.kind == "signed_in":
                    with conn.cursor() as cur:
                        cur.execute("SELECT role FROM app_user WHERE id = %s", (outcome.user_id,))
                        role = cur.fetchone()[0]
                    sid, _ = sessions.create(conn, cfg, identity.Principal(
                        user_id=outcome.user_id, permission="read", scope_namespace_id=None, role=role),
                        via=f"oidc:{pid}", identity_id=outcome.identity_id)
        except psycopg.errors.UniqueViolation:
            # two callbacks linking the same sign-in at the same moment: one wins
            return _go(f"{back}?auth=denied_taken")
        log.info("oidc %s: %s%s", pid, outcome.kind, f" ({outcome.reason})" if outcome.reason else "")
        if outcome.kind == "signed_in":
            r = _go("/memory")
            panel["set_cookie"](r, sid)
            return r
        if outcome.kind == "linked":
            return _go("/account/signins?auth=linked")
        if outcome.kind == "pending":
            return _go(f"{back}?auth=pending")
        return _go(f"{back}?auth=denied_{outcome.reason or 'no_account'}")

    # ─── a person's sign-in methods ─────────────────────────────────────────
    @app.get("/ui/api/me/signins")
    def my_signins(request: Request):
        s = panel["session"](request)
        if s.user_id is None:
            raise HTTPException(409, "the administrator token has no account")
        with pool.connection() as conn:
            return {"signins": admission.identities(conn, s.user_id, providers),
                    "providers": [{"id": p.id, "label": p.label} for p in providers.values()]}

    @app.delete("/ui/api/me/signins/{identity_id}")
    def unlink(identity_id: str, request: Request):
        s = panel["changing"](request)
        if s.user_id is None:
            raise HTTPException(409, "the administrator token has no account")
        with pool.connection() as conn, conn.transaction():
            result = admission.unlink_own(conn, s.user_id, identity_id)
        if result == "not_found":
            raise HTTPException(404, "not found")
        if result == "last":
            raise HTTPException(409, "this is your only sign-in method — an administrator can remove it")
        return {"unlinked": identity_id}

    # ─── the administrator's queue ──────────────────────────────────────────
    def _admin(request: Request, changing: bool = False):
        s = panel["changing"](request) if changing else panel["session"](request)
        if s.role not in ADMIN_ROLES:
            raise HTTPException(403, "administrators only")
        return s

    @app.get("/ui/api/admin/signin-requests")
    def signin_requests(request: Request):
        _admin(request)
        with pool.connection() as conn:
            reqs = admission.list_requests(conn)
        with pool.connection() as conn:
            for r in reqs:
                r["provider_label"] = providers[r["provider"]].label if r["provider"] in providers else r["provider"]
                # someone already expects this person: say so next to the decision
                r["invites"] = spaces.invites_for(conn, r["email"] if r["email_verified"] else None)
        return {"requests": reqs}

    @app.post("/ui/api/admin/signin-requests/{request_id}")
    def decide(request_id: str, request: Request, action: str = Body(..., embed=True),
               user_id: str = Body(None, embed=True)):
        s = _admin(request, changing=True)
        with pool.connection() as conn, conn.transaction():
            try:
                return admission.decide(conn, actor_role=s.role, actor_user_id=s.user_id,
                                        request_id=request_id, action=action, user_id=user_id)
            except admission.DecisionRefused as e:
                raise HTTPException(e.status, str(e))
