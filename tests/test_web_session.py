"""The web panel's sessions: the administrator door, cookies, CSRF, and a
session never outliving what justified it.

Against a live managed deployment through the REST app with the panel mounted.
"""

import dataclasses
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("psycopg_pool")

from fastapi.testclient import TestClient  # noqa: E402

from memgres import identity  # noqa: E402
from memgres.config import load  # noqa: E402
from memgres.server import create_app  # noqa: E402
from memgres.web import sessions  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
ORIGIN = "https://testserver"
COOKIE = "__Host-memgres_session"


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


@pytest.fixture
def box(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    root = identity.new_token()
    for k, v in {"MEMGRES_DATABASE_URL": DSN, "MEMGRES_KEY_MODE": "managed",
                 "MEMGRES_EMBED_PROVIDER": "none", "MEMGRES_FTS_LANGUAGE": "simple",
                 "MEMGRES_REQUIRE_TITLE": "false", "MEMGRES_ADMIN_TOKEN": root,
                 "MEMGRES_ADMIN_ROLE": "superadmin", "MEMGRES_WEB_ENABLED": "true", "MEMGRES_PUBLIC_URL": "https://testserver"}.items():
        monkeypatch.setenv(k, v)
    cfg = load()
    with TestClient(create_app(cfg), base_url=ORIGIN) as c:
        yield c, root, cfg


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _user(client, root, name, role="user", permission=None):
    """An account and a personal token for it — a full admin-ceiling token for an
    administrator, since that is what the administrator door accepts."""
    uid = client.post("/admin/users", json={"name": name, "role": role},
                      headers=_bearer(root)).json()["id"]
    perm = permission or ("admin" if role != "user" else "write")
    tok = client.post("/admin/tokens", json={"user_id": uid, "permission": perm},
                      headers=_bearer(root)).json()["token"]
    return uid, tok


def _sign_in(client, token, origin=ORIGIN):
    headers = {"Origin": origin} if origin else {}
    return client.post("/ui/api/session/token", json={"token": token}, headers=headers)


def _csrf(client):
    return {"Origin": ORIGIN, "X-Memgres-CSRF": client.get("/ui/api/session").json()["csrf"]}


# ─── the door ────────────────────────────────────────────────────────────────
def test_the_env_admin_token_signs_in(box):
    client, root, _ = box
    r = _sign_in(client, root)
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["can"]["admin"] is True and view["role"] == "superadmin"
    assert client.get("/ui/api/session").status_code == 200


def test_a_user_managers_personal_token_signs_in(box):
    client, root, _ = box
    _, tok = _user(client, root, "olga", role="user_manager")
    r = _sign_in(client, tok)
    assert r.status_code == 200 and r.json()["role"] == "user_manager"
    assert r.json()["user"]["name"] == "olga"


def test_a_plain_users_valid_token_is_refused_like_a_made_up_one(box):
    """This door is for the day providers fail. A refusal must not say whether
    the token exists."""
    client, root, _ = box
    _, tok = _user(client, root, "mark")
    real = _sign_in(client, tok)
    fake = _sign_in(client, identity.new_token())
    assert real.status_code == fake.status_code == 403
    assert real.json() == fake.json()
    assert COOKIE not in client.cookies


def test_a_weakened_admin_token_does_not_open_the_door(box):
    """A read-only or space-pinned token minted for an agent, on an admin
    account, must not become a session with the account's whole authority —
    from there it could mint an unpinned token and outlive its own revocation."""
    client, root, _ = box
    uid, _ = _user(client, root, "olga", role="superadmin")
    ns = client.post("/admin/namespaces", json={"owner_user_id": uid, "name": "olga"},
                     headers=_bearer(root)).json()["id"]
    weak = [client.post("/admin/tokens", json={"user_id": uid, "permission": p, "namespace_id": n},
                        headers=_bearer(root)).json()["token"]
            for p, n in (("read", None), ("write", None), ("admin", ns))]
    fake = _sign_in(client, identity.new_token())
    for tok in weak:
        r = _sign_in(client, tok)
        assert r.status_code == 403 and r.json() == fake.json()
    assert COOKIE not in client.cookies


def test_a_refused_token_is_not_marked_used(box):
    client, root, _ = box
    _, tok = _user(client, root, "mark")
    _sign_in(client, tok)
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT last_used_at FROM token WHERE token_hash = %s", (identity.token_hash(tok),))
        assert cur.fetchone()[0] is None


def test_sign_in_needs_the_panels_own_origin(box):
    client, root, _ = box
    assert _sign_in(client, root, origin=None).status_code == 403
    assert _sign_in(client, root, origin="https://evil.example").status_code == 403


def test_guessing_is_throttled(box):
    client, root, _ = box
    for _ in range(10):
        assert _sign_in(client, identity.new_token()).status_code == 403
    assert _sign_in(client, root).status_code == 429


# ─── the cookie ──────────────────────────────────────────────────────────────
def test_the_cookie_is_host_only_httponly_strict_and_secure(box):
    client, root, _ = box
    header = _sign_in(client, root).headers["set-cookie"]
    assert header.startswith(COOKIE + "=")
    low = header.lower()
    for attr in ("httponly", "secure", "samesite=strict", "path=/"):
        assert attr in low
    assert "domain=" not in low


def test_only_a_hash_of_the_session_id_is_stored(box):
    client, root, _ = box
    _sign_in(client, root)
    sid = client.cookies[COOKIE]
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id_hash FROM web_session")
        stored = [r[0] for r in cur.fetchall()]
    assert stored and sid not in stored


def test_the_rest_api_does_not_accept_the_cookie(box):
    """A signed-in browser must not make the bearer API callable by any page
    that can get the browser to send a request."""
    client, root, _ = box
    _sign_in(client, root)
    assert client.get("/whoami").status_code == 401
    assert client.get("/admin/users").status_code in (401, 403)


# ─── CSRF ────────────────────────────────────────────────────────────────────
def test_sign_out_needs_the_csrf_token(box):
    client, root, _ = box
    _sign_in(client, root)
    assert client.delete("/ui/api/session", headers={"Origin": ORIGIN}).status_code == 403
    assert client.delete("/ui/api/session",
                         headers={"Origin": ORIGIN, "X-Memgres-CSRF": "nope"}).status_code == 403
    ok = client.delete("/ui/api/session", headers=_csrf(client))
    assert ok.status_code == 200
    assert client.get("/ui/api/session").status_code == 401


def test_a_signed_out_cookie_does_not_come_back(box):
    client, root, _ = box
    _sign_in(client, root)
    sid = client.cookies[COOKIE]
    client.delete("/ui/api/session", headers=_csrf(client))
    client.cookies.set(COOKIE, sid)
    assert client.get("/ui/api/session").status_code == 401


# ─── a session never outlives its justification ──────────────────────────────
def test_revoking_the_sign_in_token_ends_the_session(box):
    client, root, _ = box
    _, tok = _user(client, root, "olga", role="user_manager")
    _sign_in(client, tok)
    with psycopg.connect(DSN) as conn:
        p = identity.resolve(conn, load(), tok)
    client.post(f"/admin/tokens/{p.token_id}/revoke", headers=_bearer(root))
    assert client.get("/ui/api/session").status_code == 401


def test_disabling_the_account_ends_the_session(box):
    client, root, _ = box
    uid, tok = _user(client, root, "olga", role="user_manager")
    _sign_in(client, tok)
    client.post(f"/admin/users/{uid}/disabled", json={"disabled": True}, headers=_bearer(root))
    assert client.get("/ui/api/session").status_code == 401


def test_losing_the_admin_role_ends_a_token_session(box):
    client, root, _ = box
    uid, tok = _user(client, root, "olga", role="user_manager")
    _sign_in(client, tok)
    r = client.post(f"/admin/users/{uid}/role", json={"role": "user"}, headers=_bearer(root))
    assert r.status_code == 200, r.text
    assert client.get("/ui/api/session").status_code == 401


def test_an_expired_session_is_refused(box):
    client, root, _ = box
    _sign_in(client, root)
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE web_session SET expires_at = now() - interval '1 minute'")
    assert client.get("/ui/api/session").status_code == 401


def test_a_root_session_ends_when_the_env_token_is_rotated(box):
    """The anonymous root has no account to re-check, so its session is bound to
    the secret itself."""
    _, root, cfg = box
    with psycopg.connect(DSN) as conn:
        p = identity.Principal(user_id=None, permission="admin",
                               scope_namespace_id=None, is_admin=True)
        sid, _ = sessions.create(conn, cfg, p, via="token")
        assert sessions.load(conn, cfg, sid) is not None
        rotated = dataclasses.replace(cfg, admin_token=identity.new_token())
        assert sessions.load(conn, rotated, sid) is None
        assert sessions.load(conn, dataclasses.replace(cfg, admin_token=""), sid) is None


def test_the_session_principal_cannot_write_memory(box):
    """The panel only reads. If a bug ever sent a write through a session, the
    read ceiling refuses it at the store."""
    from memgres.store import Store
    client, root, cfg = box
    uid, _ = _user(client, root, "ada", role="user_manager")
    client.post("/admin/namespaces", json={"owner_user_id": uid, "name": "ada"},
                headers=_bearer(root))
    with psycopg.connect(DSN) as conn:
        sid, _ = sessions.create(conn, cfg, identity.Principal(
            user_id=uid, permission="write", scope_namespace_id=None,
            role="user_manager"), via="oidc:test")
        conn.commit()
        s = sessions.load(conn, cfg, sid)
        store = Store(cfg, conn=conn)
        with pytest.raises(identity.AuthError):
            store.write(sessions.principal(s), body="x", path="a.b", space="ada")


# ─── pages and the /signin redirect ──────────────────────────────────────────
def test_signin_goes_to_the_admin_door_until_an_admin_links_a_provider(box):
    client, root, _ = box
    r = client.get("/signin", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("/signin/admin")
    assert client.get("/ui/api/signin-options").json()["admin_redirect"] is True

    uid, _ = _user(client, root, "olga", role="user_manager")
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_user_identity (issuer, subject, user_id, provider) "
                    "VALUES ('https://id.example', 'olga-1', %s, 'sso')", (uid,))
    assert client.get("/signin", follow_redirects=False).status_code == 200
    assert client.get("/ui/api/signin-options").json()["admin_redirect"] is False


def test_a_disabled_admins_link_does_not_count(box):
    client, root, _ = box
    uid, _ = _user(client, root, "olga", role="user_manager")
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_user_identity (issuer, subject, user_id, provider) "
                    "VALUES ('https://id.example', 'olga-1', %s, 'sso')", (uid,))
    client.post(f"/admin/users/{uid}/disabled", json={"disabled": True}, headers=_bearer(root))
    assert client.get("/signin", follow_redirects=False).status_code == 307


def test_pages_carry_a_strict_content_security_policy(box):
    client, _, _ = box
    for path in ("/", "/signin/admin", "/account"):
        r = client.get(path)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-frame-options"] == "DENY"


def test_api_answers_are_not_cached(box):
    client, root, _ = box
    _sign_in(client, root)
    assert client.get("/ui/api/session").headers["cache-control"] == "no-store"


# ─── language ────────────────────────────────────────────────────────────────
def test_a_person_saves_a_language(box):
    client, root, _ = box
    _, tok = _user(client, root, "olga", role="user_manager")
    _sign_in(client, tok)
    r = client.patch("/ui/api/me", json={"ui_language": "ru"}, headers=_csrf(client))
    assert r.status_code == 200
    assert client.get("/ui/api/session").json()["user"]["ui_language"] == "ru"
    bad = client.patch("/ui/api/me", json={"ui_language": "xx"}, headers=_csrf(client))
    assert bad.status_code == 422
    back = client.patch("/ui/api/me", json={"ui_language": None}, headers=_csrf(client))
    assert back.status_code == 200
    assert client.get("/ui/api/session").json()["user"]["ui_language"] is None


# ─── off by default, and not in single mode ──────────────────────────────────
def test_the_panel_is_off_unless_enabled(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    with TestClient(create_app(load())) as c:
        assert c.get("/ui/api/session").status_code == 404
        assert c.get("/signin").status_code == 404


def test_the_panel_needs_a_public_url(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    monkeypatch.setenv("MEMGRES_WEB_ENABLED", "true")
    with pytest.raises(ValueError, match="PUBLIC_URL"):
        load()


@pytest.mark.parametrize("url, ok", [("http://127.0.0.1:8080", True), ("http://localhost:8080", True),
                                     ("http://memory.lan", False), ("https://memory.example.com", False)])
def test_oidc_over_insecure_cookies_only_on_localhost(monkeypatch, url, ok):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    for k, v in {"MEMGRES_KEY_MODE": "managed", "MEMGRES_WEB_ENABLED": "true", "MEMGRES_PUBLIC_URL": url,
                 "MEMGRES_OIDC_CONFIG": "/nonexistent.toml", "MEMGRES_WEB_COOKIE_SECURE": "false"}.items():
        monkeypatch.setenv(k, v)
    if ok:
        load()
    else:
        with pytest.raises(ValueError, match="COOKIE_SECURE"):
            load()


def test_single_mode_cannot_enable_the_panel(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_WEB_ENABLED", "true")
    monkeypatch.setenv("MEMGRES_PUBLIC_URL", "https://testserver")
    with pytest.raises(ValueError, match="KEY_MODE"):
        load()
