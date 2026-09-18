"""The web panel's HTTP surface: pages, static files and ``/ui/api``.

Mounted onto the REST app by :func:`mount` when ``MEMGRES_WEB_ENABLED`` is on.

Two rules keep a browser session from widening what an attacker can reach:

* **The panel API is its own prefix, and only it reads the cookie.** The REST
  routes keep taking bearer tokens and nothing else, so a signed-in browser
  does not make them callable by any page that can send it a request.
* **Anything that changes state proves it came from the panel**: a matching
  ``Origin`` and the session's CSRF token in ``X-Memgres-CSRF``. The cookie is
  ``SameSite=Strict`` as well; that is the second lock, not the only one.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .. import identity
from ..identity import AuthError
from . import sessions

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOCALES = tuple(sorted(p.stem for p in (STATIC_DIR / "locales").glob("*.json")))

# Paths the single-page app answers itself; the server hands every one of them
# the same shell. A path not listed here is a 404, not a blank app.
APP_PATHS = ("/", "/memory", "/space", "/people", "/account", "/account/tokens", "/account/signins",
             "/admin", "/admin/people")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


class _Throttle:
    """Failed sign-ins per client, in this process.

    Deliberately small: it slows guessing down; it is not the only defence (a
    token is 256 bits) and does not replace a rate limit in front of the server.
    """

    MAX_KEYS = 10_000     # rotating addresses must not grow this without bound

    def __init__(self, limit: int = 10, window_s: float = 900.0):
        self.limit, self.window_s = limit, window_s
        self._fails: dict = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        if len(self._fails) < self.MAX_KEYS:
            return
        for k in [k for k, v in self._fails.items() if not v or now - v[-1] >= self.window_s]:
            del self._fails[k]
        while len(self._fails) >= self.MAX_KEYS:
            self._fails.pop(next(iter(self._fails)))

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._fails.get(key, []) if now - t < self.window_s]
            self._fails[key] = hits
            return len(hits) >= self.limit

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._fails.setdefault(key, []).append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


class _ScrubSignInQuery(logging.Filter):
    """Keep the query string of /ui/auth/* out of the access log.

    The provider's redirect back carries a one-time code and the state. Both are
    spent by the time the line is written and useless without the PKCE verifier,
    but a log is kept and copied, and there is no reason for them to be in it."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) \
                and args[2].startswith("/ui/auth/") and "?" in args[2]:
            record.args = args[:2] + (args[2].split("?", 1)[0] + "?…",) + args[3:]
        return True


def _scrub_access_log() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _ScrubSignInQuery) for f in access.filters):
        access.addFilter(_ScrubSignInQuery())


def mount(app, cfg, pool, make_store, *, oidc_fetch=None) -> None:
    from fastapi import Body, HTTPException, Request, Response
    from fastapi.responses import FileResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles

    from . import oidc_config
    cookie_name = "__Host-memgres_session" if cfg.web_cookie_secure else "memgres_session"
    throttle = _Throttle()
    _scrub_access_log()
    # read at startup: a broken provider file stops the server rather than
    # leaving a sign-in door that half-works
    providers = oidc_config.load(cfg.oidc_config, key_mode=cfg.key_mode)

    # ─── plumbing ───────────────────────────────────────────────────────────
    def _origin_of(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    def _allowed_origin(request: Request) -> str:
        # config refuses to start the panel without it
        return _origin_of(cfg.public_url)

    def _require_same_origin(request: Request) -> None:
        offered = request.headers.get("origin")
        if not offered or offered.lower() != _allowed_origin(request):
            raise HTTPException(403, "cross-origin request refused")

    def _session(request: Request):
        with pool.connection() as conn:
            return sessions.load(conn, cfg, request.cookies.get(cookie_name))

    def _signed_in(request: Request):
        s = _session(request)
        if s is None:
            raise HTTPException(401, "not signed in")
        return s

    def _changing(request: Request):
        """A signed-in, same-origin, CSRF-carrying request — every state change."""
        _require_same_origin(request)
        s = _signed_in(request)
        if not sessions.csrf_ok(s, request.headers.get("x-memgres-csrf")):
            raise HTTPException(403, "missing or wrong CSRF token")
        return s

    def _set_cookie(response: Response, sid: str) -> None:
        response.set_cookie(cookie_name, sid, max_age=cfg.web_session_hours * 3600,
                            path="/", secure=cfg.web_cookie_secure, httponly=True,
                            samesite="strict")

    def _session_view(s) -> dict:
        pending = 0
        with pool.connection() as conn:
            me = sessions.profile(conn, s.user_id)
            if s.role in identity.ADMIN_ROLES:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM signin_request WHERE status = 'pending'")
                    pending = cur.fetchone()[0]
        return {
            "pending_requests": pending,
            "user": me,
            "role": s.role,
            "via": s.via,
            "csrf": s.csrf,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "can": {
                # the anonymous root owns nothing: no memory to browse, no
                # account to hold tokens — only the admin area
                "memory": s.user_id is not None,
                "tokens": s.user_id is not None,
                "admin": s.role in identity.ADMIN_ROLES,
            },
            "locales": list(LOCALES),
            # where agents connect, for the token dialogs (the panel cannot work it out)
            "mcp_url": cfg.mcp_public_url or None,
        }

    def _client_key(request: Request) -> str:
        return request.client.host if request.client else "?"

    @app.middleware("http")
    async def _panel_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path in APP_PATHS or path.startswith(("/ui/", "/signin")):
            response.headers.setdefault("Content-Security-Policy", CSP)
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
        if path.startswith("/ui/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ─── pages ──────────────────────────────────────────────────────────────
    shell = STATIC_DIR / "index.html"

    def _page():
        return FileResponse(shell, media_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    # HEAD too: a page answers HEAD as it answers GET (RFC 9110), and uptime
    # checks use it — a 405 there reads as "the panel is down"
    for p in APP_PATHS:
        app.add_api_route(p, _page, methods=["GET", "HEAD"], include_in_schema=False)

    @app.api_route("/signin", methods=["GET", "HEAD"], include_in_schema=False)
    def signin_page():
        with pool.connection() as conn:
            if not sessions.admin_has_linked_signin(conn):
                return RedirectResponse("/signin/admin?from=signin", status_code=307)
        return _page()

    @app.api_route("/signin/admin", methods=["GET", "HEAD"], include_in_schema=False)
    def signin_admin_page():
        return _page()

    app.mount("/ui/static", StaticFiles(packages=[("memgres.web", "static")]),
              name="panel-static")

    # ─── session ────────────────────────────────────────────────────────────
    @app.get("/ui/api/session")
    def get_session(request: Request):
        return _session_view(_signed_in(request))

    @app.post("/ui/api/session/token")
    def sign_in_with_token(request: Request, response: Response,
                           token: str = Body(..., embed=True)):
        """The administrator door. The secret arrives in a JSON body — never a
        query string, which ends up in logs and browser history."""
        _require_same_origin(request)
        key = _client_key(request)
        if throttle.blocked(key):
            raise HTTPException(429, "too many failed attempts — wait a few minutes")
        with pool.connection() as conn, conn.transaction():
            try:
                p = sessions.authenticate_admin_token(conn, cfg, token.strip())
            except AuthError as e:
                throttle.fail(key)
                raise HTTPException(403, str(e))
            sid, _ = sessions.create(conn, cfg, p, via="token")
        throttle.clear(key)
        _set_cookie(response, sid)
        with pool.connection() as conn:
            return _session_view(sessions.load(conn, cfg, sid))

    @app.delete("/ui/api/session")
    def sign_out(request: Request, response: Response):
        _changing(request)
        with pool.connection() as conn:
            sessions.end(conn, request.cookies.get(cookie_name))
        response.delete_cookie(cookie_name, path="/", secure=cfg.web_cookie_secure,
                               httponly=True, samesite="strict")
        return {"signed_out": True}

    @app.get("/ui/api/signin-options")
    def signin_options():
        """What the sign-in screen offers. Providers arrive with OIDC; until an
        administrator links one, the screen sends people to the admin door."""
        with pool.connection() as conn:
            linked = sessions.admin_has_linked_signin(conn)
        return {"providers": [{"id": p.id, "label": p.label, "hint": p.hint} for p in providers.values()],
                "admin_redirect": not linked, "locales": list(LOCALES)}

    # ─── the person ─────────────────────────────────────────────────────────
    @app.patch("/ui/api/me")
    def update_me(request: Request, ui_language: Optional[str] = Body(None, embed=True)):
        s = _changing(request)
        if s.user_id is None:
            raise HTTPException(409, "the administrator token has no account to save this to")
        if ui_language is not None and ui_language not in LOCALES:
            raise HTTPException(422, f"unknown language {ui_language!r}; "
                                     f"available: {', '.join(LOCALES)}")
        with pool.connection() as conn:
            sessions.set_language(conn, s.user_id, ui_language)
        return _session_view(s)

    # the other modules of the panel attach here
    app.state.panel = {"session": _signed_in, "changing": _changing,
                       "principal": sessions.principal, "cookie_name": cookie_name,
                       "set_cookie": _set_cookie, "client_key": _client_key,
                       "same_origin": _require_same_origin}

    from . import auth_routes, memory, people, spaces, tokens
    tokens.mount(app, cfg, pool, app.state.panel)
    memory.mount(app, cfg, pool, app.state.panel, make_store)
    spaces.mount(app, cfg, pool, app.state.panel)
    people.mount(app, cfg, pool, app.state.panel, providers, make_store)
    auth_routes.mount(app, cfg, pool, app.state.panel, providers, fetch=oidc_fetch)
