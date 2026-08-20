"""Service roles + first-admin bootstrap, against a live Postgres.

Covers the seed (env / token-file read-or-create), the zero-admins invariant and
idempotency, attribution of a seeded token to its real user, the superadmin
data-root vs. a user_manager's lack of cross-tenant access, grant/revoke with
anti-lockout, and the REST role gating.
"""

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")

from memgres import bootstrap as bs  # noqa: E402
from memgres import identity  # noqa: E402
from memgres.config import load  # noqa: E402
from memgres.schema import migrate  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


@pytest.fixture
def env(monkeypatch):
    """Fresh schema + a clean MEMGRES_ env pointed at the test DB (managed)."""
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    return monkeypatch


def _conn():
    return psycopg.connect(DSN, autocommit=True)


# ─── bootstrap seeding ───────────────────────────────────────────────────────
def test_seeds_user_manager_by_default_and_is_idempotent(env):
    tok = identity.new_token()
    env.setenv("MEMGRES_ADMIN_TOKEN", tok)
    cfg = load()
    with _conn() as conn:
        migrate(conn, cfg)
        uid = bs.bootstrap_admin(conn, cfg)
        assert uid and identity.count_service_admins(conn) == 1
        assert identity.get_role(conn, uid) == "user_manager"

        # the env token now resolves to that REAL user — attributed, not anon root
        p = identity.resolve(conn, cfg, tok)
        assert p.user_id == uid and p.role == "user_manager"
        assert p.is_admin is False and identity.can_manage_users(p)

        # inert on a second call, and even if the env token changes
        assert bs.bootstrap_admin(conn, cfg) is None
        env.setenv("MEMGRES_ADMIN_TOKEN", identity.new_token())
        assert bs.bootstrap_admin(conn, load()) is None
        assert identity.count_service_admins(conn) == 1


def test_seeds_superadmin_with_full_data_root(env):
    env.setenv("MEMGRES_ADMIN_ROLE", "superadmin")
    tok = identity.new_token()
    env.setenv("MEMGRES_ADMIN_TOKEN", tok)
    cfg = load()
    with _conn() as conn:
        migrate(conn, cfg)
        bs.bootstrap_admin(conn, cfg)
        # a separate tenant with a private namespace
        other = identity.create_user(conn, "tenant")
        ns = identity.create_namespace(conn, other, "private")

        p = identity.resolve(conn, cfg, tok)
        assert p.is_admin and p.role == "superadmin"
        # reaches ANY namespace by id, as admin (capped by the token ceiling)
        nsid, perm = identity.resolve_space(conn, p, space_id=ns)
        assert nsid == ns and perm == "admin"


def test_user_manager_has_no_cross_tenant_data_access(env):
    tok = identity.new_token()               # default role: user_manager
    env.setenv("MEMGRES_ADMIN_TOKEN", tok)
    cfg = load()
    with _conn() as conn:
        migrate(conn, cfg)
        bs.bootstrap_admin(conn, cfg)
        other = identity.create_user(conn, "tenant")
        ns = identity.create_namespace(conn, other, "private")

        p = identity.resolve(conn, cfg, tok)
        assert identity.can_manage_users(p) and not p.is_admin
        with pytest.raises(identity.SpaceNotFound):
            identity.resolve_space(conn, p, space_id=ns)


def test_token_file_is_generated_0600_when_absent(env, tmp_path):
    path = tmp_path / "initial_admin_token"
    env.setenv("MEMGRES_ADMIN_TOKEN_FILE", str(path))
    cfg = load()
    with _conn() as conn:
        migrate(conn, cfg)
        uid = bs.bootstrap_admin(conn, cfg)
        assert uid
        secret = path.read_text().strip()
        assert identity.valid_format(secret)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert identity.resolve(conn, cfg, secret).user_id == uid


def test_token_file_existing_secret_is_used(env, tmp_path):
    tok = identity.new_token()
    path = tmp_path / "initial_admin_token"
    path.write_text(tok + "\n")
    env.setenv("MEMGRES_ADMIN_TOKEN_FILE", str(path))
    cfg = load()
    with _conn() as conn:
        migrate(conn, cfg)
        uid = bs.bootstrap_admin(conn, cfg)
        assert identity.resolve(conn, cfg, tok).user_id == uid
        assert path.read_text().strip() == tok        # left untouched


def test_non_mgk_env_token_skips_seed_but_still_works_as_root(env):
    env.setenv("MEMGRES_ADMIN_TOKEN", "legacy-plain-secret")
    cfg = load()
    with _conn() as conn:
        migrate(conn, cfg)
        assert bs.bootstrap_admin(conn, cfg) is None
        assert identity.count_service_admins(conn) == 0
        p = identity.resolve(conn, cfg, "legacy-plain-secret")
        assert p.is_admin and p.user_id is None       # anonymous break-glass


@pytest.mark.parametrize("mode", ["single", "open"])
def test_bootstrap_noop_outside_managed(env, mode):
    env.setenv("MEMGRES_KEY_MODE", mode)
    env.setenv("MEMGRES_ADMIN_TOKEN", identity.new_token())
    cfg = load()
    with _conn() as conn:
        migrate(conn, cfg)
        assert bs.bootstrap_admin(conn, cfg) is None
        assert identity.count_service_admins(conn) == 0


# ─── role management + anti-lockout ──────────────────────────────────────────
def test_grant_revoke_superadmin_anti_lockout(env):
    env.setenv("MEMGRES_ADMIN_ROLE", "superadmin")
    tok = identity.new_token()
    env.setenv("MEMGRES_ADMIN_TOKEN", tok)
    cfg = load()
    with _conn() as conn:
        migrate(conn, cfg)
        a = bs.bootstrap_admin(conn, cfg)

        # the only superadmin can't be revoked
        with pytest.raises(identity.AuthError):
            identity.revoke_superadmin(conn, a)

        b = identity.create_user(conn, "b")
        identity.grant_superadmin(conn, b)
        assert identity.count_superadmins(conn) == 2

        identity.revoke_superadmin(conn, a)              # now allowed
        assert identity.get_role(conn, a) == "user"
        assert identity.count_superadmins(conn) == 1

        with pytest.raises(identity.AuthError):          # b is the last one again
            identity.revoke_superadmin(conn, b)


# ─── REST role gating ────────────────────────────────────────────────────────
@pytest.fixture
def managed_client(env):
    pytest.importorskip("fastapi")
    pytest.importorskip("psycopg_pool")
    from fastapi.testclient import TestClient

    from memgres.server import create_app

    env.setenv("MEMGRES_ADMIN_ROLE", "superadmin")
    tok = identity.new_token()
    env.setenv("MEMGRES_ADMIN_TOKEN", tok)
    app = create_app(load())                              # bootstrap seeds at startup
    with TestClient(app) as c:
        yield c, tok


def test_rest_role_gating(managed_client):
    c, admin_tok = managed_client
    H = {"Authorization": f"Bearer {admin_tok}"}

    # superadmin mints a user_manager and a token for it
    mgr = c.post("/admin/users", json={"name": "mgr", "role": "user_manager"},
                 headers=H).json()["id"]
    mtok = c.post("/admin/tokens", json={"user_id": mgr, "permission": "admin"},
                  headers=H).json()["token"]
    Hm = {"Authorization": f"Bearer {mtok}"}

    # a user_manager may create plain users …
    assert c.post("/admin/users", json={"name": "u"}, headers=Hm).status_code == 201
    # … but may NOT mint an admin-role user (no privilege escalation) …
    assert c.post("/admin/users", json={"name": "x", "role": "superadmin"},
                  headers=Hm).status_code == 403
    # … nor grant the superadmin role.
    assert c.post(f"/admin/users/{mgr}/grant-superadmin",
                  headers=Hm).status_code == 403

    # a superadmin can promote, then anti-lockout is enforced on the last one
    assert c.post(f"/admin/users/{mgr}/grant-superadmin",
                  headers=H).status_code == 200
    # demote the manager back (two supers now → allowed)
    assert c.post(f"/admin/users/{mgr}/revoke-superadmin", json={},
                  headers=H).status_code == 200

    # no auth at all → 403
    assert c.post("/admin/users", json={"name": "z"}).status_code == 403
