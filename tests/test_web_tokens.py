"""Tokens a person issues for their own clients from the panel."""

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
from memgres.web import sessions, tokens  # noqa: E402

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


def _account(client, root, name):
    return client.post("/admin/users", json={"name": name}, headers=_bearer(root)).json()["id"]


def _signed_in_as(client, cfg, uid):
    """A session as a provider sign-in would open it — no token involved."""
    with psycopg.connect(DSN) as conn:
        sid, _ = sessions.create(conn, cfg, identity.Principal(
            user_id=uid, permission="read", scope_namespace_id=None), via="oidc:test")
    client.cookies.set(COOKIE, sid)
    return {"Origin": ORIGIN, "X-Memgres-CSRF": client.get("/ui/api/session").json()["csrf"]}


def test_a_plain_user_issues_a_working_token(box):
    client, root, cfg = box
    uid = _account(client, root, "mark")
    h = _signed_in_as(client, cfg, uid)
    assert client.get("/ui/api/tokens").json()["tokens"] == []
    r = client.post("/ui/api/tokens", json={"label": "MacBook · Claude Code",
                                            "permission": "write", "expires_days": 90}, headers=h)
    assert r.status_code == 201, r.text
    secret = r.json()["token"]
    assert identity.valid_format(secret)
    who = client.get("/whoami", headers=_bearer(secret))
    assert who.status_code == 200 and who.json()["user_id"] == uid
    listed = client.get("/ui/api/tokens").json()["tokens"]
    assert len(listed) == 1 and listed[0]["label"] == "MacBook · Claude Code"
    assert listed[0]["state"] == "active" and listed[0]["expires_at"]
    assert secret not in client.get("/ui/api/tokens").text


def test_no_admin_ceiling_from_a_browser(box):
    client, root, cfg = box
    h = _signed_in_as(client, cfg, _account(client, root, "mark"))
    r = client.post("/ui/api/tokens", json={"permission": "admin", "expires_days": 30}, headers=h)
    assert r.status_code == 422


@pytest.mark.parametrize("days", [0, 1, 45, 366, 10000])
def test_the_expiry_is_one_of_the_choices(box, days):
    client, root, cfg = box
    h = _signed_in_as(client, cfg, _account(client, root, "mark"))
    r = client.post("/ui/api/tokens", json={"permission": "read", "expires_days": days}, headers=h)
    assert r.status_code == 422


def test_scope_only_to_a_reachable_space(box):
    client, root, cfg = box
    mark = _account(client, root, "mark")
    olga = _account(client, root, "olga")
    marks = client.post("/admin/namespaces", json={"owner_user_id": mark, "name": "sales"},
                        headers=_bearer(root)).json()["id"]
    olgas = client.post("/admin/namespaces", json={"owner_user_id": olga, "name": "hr"},
                        headers=_bearer(root)).json()["id"]
    h = _signed_in_as(client, cfg, mark)
    theirs = client.post("/ui/api/tokens", json={"namespace_id": olgas, "expires_days": 30}, headers=h)
    made_up = client.post("/ui/api/tokens", json={"namespace_id": "11111111-1111-1111-1111-111111111111",
                                                  "expires_days": 30}, headers=h)
    garbage = client.post("/ui/api/tokens", json={"namespace_id": "nope", "expires_days": 30}, headers=h)
    assert theirs.status_code == made_up.status_code == garbage.status_code == 422
    assert theirs.json() == made_up.json() == garbage.json()
    ok = client.post("/ui/api/tokens", json={"namespace_id": marks, "expires_days": 30}, headers=h)
    assert ok.status_code == 201
    assert client.get("/ui/api/tokens").json()["tokens"][0]["namespace"] == "sales"


def test_revoke_your_own_but_not_someone_elses(box):
    client, root, cfg = box
    mark, olga = _account(client, root, "mark"), _account(client, root, "olga")
    h_olga = _signed_in_as(client, cfg, olga)
    olgas = client.post("/ui/api/tokens", json={"expires_days": 30}, headers=h_olga).json()
    h_mark = _signed_in_as(client, cfg, mark)
    marks = client.post("/ui/api/tokens", json={"expires_days": 30}, headers=h_mark).json()

    assert client.delete(f"/ui/api/tokens/{olgas['id']}", headers=h_mark).status_code == 404
    assert client.get("/whoami", headers=_bearer(olgas["token"])).status_code == 200
    assert client.delete("/ui/api/tokens/not-a-uuid", headers=h_mark).status_code == 404

    assert client.delete(f"/ui/api/tokens/{marks['id']}", headers=h_mark).status_code == 200
    assert client.get("/whoami", headers=_bearer(marks["token"])).status_code in (401, 403)
    assert client.get("/ui/api/tokens").json()["tokens"][0]["state"] == "revoked"


def test_issuing_needs_the_csrf_token_and_origin(box):
    client, root, cfg = box
    h = _signed_in_as(client, cfg, _account(client, root, "mark"))
    assert client.post("/ui/api/tokens", json={"expires_days": 30},
                       headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/ui/api/tokens", json={"expires_days": 30},
                       headers={**h, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/ui/api/tokens").json()["tokens"] == []


def test_the_root_session_holds_no_tokens(box):
    client, root, cfg = box
    client.post("/ui/api/session/token", json={"token": root}, headers={"Origin": ORIGIN})
    with psycopg.connect(DSN) as conn:
        # force the anonymous root: sign in as a principal with no account
        sid, _ = sessions.create(conn, cfg, identity.Principal(
            user_id=None, permission="admin", scope_namespace_id=None, is_admin=True), via="token")
    client.cookies.set(COOKIE, sid)
    assert client.get("/ui/api/tokens").status_code == 409


def test_a_cap_on_live_tokens(box, monkeypatch):
    client, root, cfg = box
    # the cap lives in the service layer now, so every door obeys the same one
    from memgres import admin
    monkeypatch.setattr(admin, "SELF_MAX_LIVE_TOKENS", 3)
    h = _signed_in_as(client, cfg, _account(client, root, "mark"))
    for _ in range(3):
        assert client.post("/ui/api/tokens", json={"expires_days": 30}, headers=h).status_code == 201
    assert client.post("/ui/api/tokens", json={"expires_days": 30}, headers=h).status_code == 422


def test_a_disabled_account_cannot_issue(box):
    client, root, cfg = box
    uid = _account(client, root, "mark")
    h = _signed_in_as(client, cfg, uid)
    client.post(f"/admin/users/{uid}/disabled", json={"disabled": True}, headers=_bearer(root))
    assert client.post("/ui/api/tokens", json={"expires_days": 30}, headers=h).status_code == 401
