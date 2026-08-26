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
    # Captions are not what most of this suite is about; the requirement
    # has its own file (test_require_title.py) covering both settings.
    monkeypatch.setenv("MEMGRES_REQUIRE_TITLE", "false")
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


def test_user_manager_cannot_reach_an_admin_account(managed_client):
    """The escalation the service layer was extracted to close.

    A user_manager hands out access without gaining it. Provisioning was gated
    on the caller's role but never on the *target's*, and every one of these
    routes takes the target as a parameter — so a manager could mint itself a
    token for the superadmin's account and be root in one request.
    """
    c, admin_tok = managed_client
    H = {"Authorization": f"Bearer {admin_tok}"}

    mgr = c.post("/admin/users", json={"name": "mgr", "role": "user_manager"},
                 headers=H).json()["id"]
    mtok = c.post("/admin/tokens", json={"user_id": mgr, "permission": "admin"},
                  headers=H).json()["token"]
    Hm = {"Authorization": f"Bearer {mtok}"}

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM app_user WHERE role='superadmin'")
        root = str(cur.fetchone()[0])
    root_tokens = c.get(f"/admin/users/{root}/tokens", headers=H).json()

    # the escalation: a fresh credential on the root account
    assert c.post("/admin/tokens", json={"user_id": root, "permission": "admin"},
                  headers=Hm).status_code == 403
    # the lockout: destroying the only credential that can undo any of this
    assert c.post(f"/admin/tokens/{root_tokens[0]['id']}/revoke",
                  headers=Hm).status_code == 403
    # not even reconnaissance
    assert c.get(f"/admin/users/{root}/tokens", headers=Hm).status_code == 403

    # plain users stay fully manageable — the refusal is targeted, not a ban on
    # the tier, or a user_manager could no longer do its job.
    u = c.post("/admin/users", json={"name": "u"}, headers=Hm).json()["id"]
    issued = c.post("/admin/tokens", json={"user_id": u}, headers=Hm)
    assert issued.status_code == 201
    assert c.get(f"/admin/users/{u}/tokens", headers=Hm).status_code == 200
    assert c.post(f"/admin/tokens/{issued.json()['id']}/revoke",
                  headers=Hm).status_code == 200


def test_a_weakened_token_does_not_open_the_control_plane(managed_client):
    """A role says who someone IS; a token says what THIS credential may do.

    The recommended way to give an agent a narrow credential is a read-only,
    namespace-scoped token. If the control plane consults only the role behind
    it, that narrowing is decorative: the weak token opens provisioning, issues
    itself a strong one, and the pin is gone. The data plane already honoured the
    token; this pins that the control plane does too.
    """
    c, admin_tok = managed_client
    H = {"Authorization": f"Bearer {admin_tok}"}

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM app_user WHERE role='superadmin'")
        root = str(cur.fetchone()[0])
    ns = c.post("/admin/namespaces",
                json={"owner_user_id": root, "name": "vault"},
                headers=H).json()["id"]

    # two deliberately-weakened credentials on the ROOT account itself
    weak = c.post("/admin/tokens", json={"user_id": root, "permission": "read"},
                  headers=H).json()["token"]
    pinned = c.post("/admin/tokens",
                    json={"user_id": root, "permission": "admin",
                          "namespace_id": ns},
                    headers=H).json()["token"]

    for tok, why in ((weak, "read-only"), (pinned, "namespace-scoped")):
        Hw = {"Authorization": f"Bearer {tok}"}
        # the escalation this closes: minting a full credential for itself
        assert c.post("/admin/tokens",
                      json={"user_id": root, "permission": "admin"},
                      headers=Hw).status_code == 403, why
        assert c.post("/admin/users", json={"name": "mule"},
                      headers=Hw).status_code == 403, why
        assert c.post(f"/admin/users/{root}/grant-superadmin",
                      headers=Hw).status_code == 403, why
        # …while the identity itself still reads back fine
        assert c.get("/whoami", headers=Hw).status_code == 200, why

    # a scoped token may still administer the namespace it IS scoped to
    Hp = {"Authorization": f"Bearer {pinned}"}
    assert c.get(f"/spaces/{ns}/access-requests", headers=Hp).status_code == 200


def test_a_profile_makes_authorship_readable(managed_client):
    """`app_user.name` was doing two jobs — the handle a token resolves to, and
    the thing a person reads in blame. It is neither unique nor required, so an
    audit line could come back as a bare uuid, which nobody can act on."""
    c, admin_tok = managed_client
    H = {"Authorization": f"Bearer {admin_tok}"}

    uid = c.post("/admin/users",
                 json={"name": "ada", "email": "ada@example.com",
                       "full_name": "Ada Lovelace", "department": "Analytical",
                       "position": "Engineer"}, headers=H).json()["id"]
    ns = c.post("/admin/namespaces",
                json={"owner_user_id": uid, "name": "work"}, headers=H).json()["id"]
    tok = c.post("/admin/tokens", json={"user_id": uid, "permission": "write"},
                 headers=H).json()["token"]
    Ha = {"Authorization": f"Bearer {tok}"}

    mid = c.post("/memories", json={"body": "a line\n"}, headers=Ha).json()["id"]
    [row] = c.get(f"/memories/{mid}/history", headers=Ha).json()
    assert row["author_name"] == "Ada Lovelace"      # not the handle, not a uuid
    assert row["author_email"] == "ada@example.com"

    # the directory carries the rest, and a partial edit leaves it alone
    assert c.patch(f"/admin/users/{uid}", json={"position": "Lead Engineer"},
                   headers=H).status_code == 200
    [ada] = [u for u in c.get("/admin/users", headers=H).json() if u["id"] == uid]
    assert ada["position"] == "Lead Engineer"
    assert ada["department"] == "Analytical"          # untouched by the edit
    assert ada["full_name"] == "Ada Lovelace"

    # email is the future login, so a duplicate is refused
    assert c.post("/admin/users", json={"name": "other",
                                        "email": "ADA@example.com"},
                  headers=H).status_code == 400
