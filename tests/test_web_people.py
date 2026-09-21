"""Spaces and people in the panel: members, invitations, requests to join,
profiles, activity and the administrators' directory — and that each of them
answers only the people it is meant for."""

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
from memgres.web import sessions, spaces  # noqa: E402

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


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


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
                 "MEMGRES_ADMIN_ROLE": "superadmin", "MEMGRES_WEB_ENABLED": "true",
                 "MEMGRES_PUBLIC_URL": ORIGIN}.items():
        monkeypatch.setenv(k, v)
    cfg = load()
    with TestClient(create_app(cfg), base_url=ORIGIN) as c:
        mk = lambda name, **kw: c.post("/admin/users", json={"name": name, **kw},  # noqa: E731
                                       headers=_bearer(root)).json()["id"]
        ids = {
            "mark": mk("mark", email="mark@example.com", full_name="Mark Levin", department="Sales"),
            "olga": mk("olga", email="olga@example.com", full_name="Olga Petrova"),
            "ivan": mk("ivan", email="ivan@example.com"),
            "mgr": mk("mgr", role="user_manager"),
            "boss": mk("boss", role="superadmin"),
        }
        ids["sales"] = c.post("/admin/namespaces", json={"owner_user_id": ids["mark"], "name": "sales"},
                              headers=_bearer(root)).json()["id"]
        ids["hr"] = c.post("/admin/namespaces", json={"owner_user_id": ids["olga"], "name": "hr"},
                           headers=_bearer(root)).json()["id"]
        tok = lambda uid: c.post("/admin/tokens", json={"user_id": uid},  # noqa: E731
                                 headers=_bearer(root)).json()["token"]
        mark_tok, olga_tok = tok(ids["mark"]), tok(ids["olga"])
        w = lambda t, **kw: c.post("/memories", json=kw, headers=_bearer(t))  # noqa: E731
        assert w(mark_tok, space="sales", path="leads.qualify", body="Budget first.").status_code == 201
        assert w(mark_tok, space="sales", path="deals.stages", body="Five stages.").status_code == 201
        assert w(olga_tok, space="hr", path="policies.vacation", body="Two weeks notice.").status_code == 201
        ids["tokens"] = {"mark": mark_tok, "olga": olga_tok}
        yield c, cfg, root, ids


def _as(client, cfg, uid):
    client.cookies.clear()
    with psycopg.connect(DSN) as conn:
        sid, _ = sessions.create(conn, cfg, identity.Principal(
            user_id=uid, permission="read", scope_namespace_id=None), via="oidc:test")
    client.cookies.set(COOKIE, sid)
    return {"Origin": ORIGIN, "X-Memgres-CSRF": client.get("/ui/api/session").json()["csrf"]}


def _share(client, root, space, uid, permission):
    r = client.post(f"/admin/namespaces/{space}/members", json={"user_id": uid, "permission": permission},
                    headers=_bearer(root))
    assert r.status_code in (200, 201), r.text


def _members(client, space):
    return {m["id"]: m["permission"] for m in client.get(f"/ui/api/spaces/{space}/members").json()["members"]}


# ─── members ─────────────────────────────────────────────────────────────────
def test_the_owner_sees_members_invites_and_requests(box):
    client, cfg, _, ids = box
    _as(client, cfg, ids["mark"])
    got = client.get(f"/ui/api/spaces/{ids['sales']}/members").json()
    assert got["members"][0]["id"] == ids["mark"] and got["members"][0]["owner"]
    assert got["invites"] == [] and got["requests"] == [] and got["can_transfer"]


@pytest.mark.parametrize("permission", [None, "read", "write"])
def test_only_space_admins_see_who_is_in_it(box, permission):
    client, cfg, root, ids = box
    if permission:
        _share(client, root, ids["sales"], ids["olga"], permission)
    _as(client, cfg, ids["olga"])
    r = client.get(f"/ui/api/spaces/{ids['sales']}/members")
    assert r.status_code == 404
    assert client.get("/ui/api/spaces/7b0c3a1e-0000-4000-8000-000000000000/members").status_code == 404


def test_an_admin_member_manages_members(box):
    client, cfg, root, ids = box
    _share(client, root, ids["sales"], ids["olga"], "admin")
    h = _as(client, cfg, ids["olga"])
    assert client.post(f"/ui/api/spaces/{ids['sales']}/members", json={"email": "ivan@example.com",
                                                                         "permission": "write"}, headers=h).status_code == 200
    assert _members(client, ids["sales"])[ids["ivan"]] == "write"
    assert client.patch(f"/ui/api/spaces/{ids['sales']}/members/{ids['ivan']}", json={"permission": "admin"},
                        headers=h).status_code == 200
    assert _members(client, ids["sales"])[ids["ivan"]] == "admin"
    assert client.delete(f"/ui/api/spaces/{ids['sales']}/members/{ids['ivan']}", headers=h).status_code == 200
    assert ids["ivan"] not in _members(client, ids["sales"])
    # but not the owner
    assert client.patch(f"/ui/api/spaces/{ids['sales']}/members/{ids['mark']}", json={"permission": "read"},
                        headers=h).status_code == 409
    assert client.delete(f"/ui/api/spaces/{ids['sales']}/members/{ids['mark']}", headers=h).status_code == 409
    # nor hand the space over
    assert client.post(f"/ui/api/spaces/{ids['sales']}/transfer", json={"user_id": ids["olga"]},
                       headers=h).status_code == 403


def test_a_write_member_cannot_add_anyone(box):
    client, cfg, root, ids = box
    _share(client, root, ids["sales"], ids["olga"], "write")
    h = _as(client, cfg, ids["olga"])
    r = client.post(f"/ui/api/spaces/{ids['sales']}/members", json={"email": "ivan@example.com"}, headers=h)
    assert r.status_code == 404
    assert identity.reaches(psycopg.connect(DSN), ids["ivan"], ids["sales"]) is None


def test_member_changes_need_the_csrf_token(box):
    client, cfg, _, ids = box
    _as(client, cfg, ids["mark"])
    r = client.post(f"/ui/api/spaces/{ids['sales']}/members", json={"email": "ivan@example.com"},
                    headers={"Origin": ORIGIN})
    assert r.status_code == 403


def test_anyone_may_leave_but_the_owner(box):
    client, cfg, root, ids = box
    _share(client, root, ids["sales"], ids["olga"], "read")
    h = _as(client, cfg, ids["olga"])
    assert client.delete(f"/ui/api/spaces/{ids['sales']}/members/{ids['olga']}", headers=h).status_code == 200
    assert [s["name"] for s in client.get("/ui/api/spaces").json()["spaces"]] == ["hr"]
    h = _as(client, cfg, ids["mark"])
    assert client.delete(f"/ui/api/spaces/{ids['sales']}/members/{ids['mark']}", headers=h).status_code == 409


def test_a_read_member_cannot_remove_someone_else(box):
    client, cfg, root, ids = box
    _share(client, root, ids["sales"], ids["olga"], "read")
    _share(client, root, ids["sales"], ids["ivan"], "read")
    h = _as(client, cfg, ids["olga"])
    assert client.delete(f"/ui/api/spaces/{ids['sales']}/members/{ids['ivan']}", headers=h).status_code == 404


def test_the_owner_hands_the_space_to_a_member(box):
    client, cfg, root, ids = box
    h = _as(client, cfg, ids["mark"])
    # not a member yet: refused
    assert client.post(f"/ui/api/spaces/{ids['sales']}/transfer", json={"user_id": ids["olga"]},
                       headers=h).status_code == 422
    _share(client, root, ids["sales"], ids["olga"], "write")
    r = client.post(f"/ui/api/spaces/{ids['sales']}/transfer", json={"user_id": ids["olga"], "keep_me": True},
                    headers=h)
    assert r.status_code == 200, r.text
    with psycopg.connect(DSN) as conn:
        assert identity.namespace_owner(conn, ids["sales"]) == ids["olga"]
        assert identity.reaches(conn, ids["mark"], ids["sales"]) == "admin"


# ─── invitations ─────────────────────────────────────────────────────────────
def test_an_invitation_answers_the_same_with_or_without_an_account(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mark"])
    known = client.post(f"/ui/api/spaces/{ids['sales']}/members", json={"email": "IVAN@example.com"}, headers=h)
    unknown = client.post(f"/ui/api/spaces/{ids['sales']}/members", json={"email": "new@example.com"}, headers=h)
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    got = client.get(f"/ui/api/spaces/{ids['sales']}/members").json()
    assert ids["ivan"] in {m["id"] for m in got["members"]}
    assert [i["email"] for i in got["invites"]] == ["new@example.com"]


def test_inviting_again_updates_the_invitation_and_it_can_be_cancelled(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mark"])
    url = f"/ui/api/spaces/{ids['sales']}/members"
    client.post(url, json={"email": "new@example.com", "permission": "read"}, headers=h)
    client.post(url, json={"email": "New@Example.com", "permission": "write"}, headers=h)
    invites = client.get(url).json()["invites"]
    assert len(invites) == 1 and invites[0]["permission"] == "write"
    assert client.delete(f"/ui/api/spaces/{ids['sales']}/invites/{invites[0]['id']}", headers=h).status_code == 200
    assert client.get(url).json()["invites"] == []


def test_a_bad_email_is_refused(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mark"])
    r = client.post(f"/ui/api/spaces/{ids['sales']}/members", json={"email": "not an email"}, headers=h)
    assert r.status_code == 422


def test_invitations_per_space_are_capped(box, monkeypatch):
    client, cfg, _, ids = box
    monkeypatch.setattr(spaces, "MAX_OPEN_INVITES", 2)
    h = _as(client, cfg, ids["mark"])
    url = f"/ui/api/spaces/{ids['sales']}/members"
    for n in range(2):
        assert client.post(url, json={"email": f"p{n}@example.com"}, headers=h).status_code == 200
    assert client.post(url, json={"email": "p9@example.com"}, headers=h).status_code == 409


def _invite_row(ids, email, inviter, permission="write"):
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO space_invite (namespace_id, email, permission, invited_by, expires_at) "
                    "VALUES (%s, %s, %s, %s, now() + interval '1 day')", (ids["sales"], email, permission, inviter))


def _new_user(email):
    with psycopg.connect(DSN) as conn:
        return identity.create_user(conn, name="newbie", email=email)


def test_an_invitation_applies_to_a_verified_address(box):
    _, _, _, ids = box
    _invite_row(ids, "new@example.com", ids["mark"])
    uid = _new_user("new@example.com")
    with psycopg.connect(DSN) as conn:
        assert spaces.apply_invites(conn, uid, None) == 0            # nothing verified: nothing applies
        assert spaces.apply_invites(conn, uid, "NEW@example.com") == 1
        assert identity.reaches(conn, uid, ids["sales"]) == "write"
        assert spaces.apply_invites(conn, uid, "new@example.com") == 0   # used once


def test_an_invitation_lapses_with_its_senders_authority(box):
    client, _, root, ids = box
    _share(client, root, ids["sales"], ids["olga"], "admin")
    _invite_row(ids, "new@example.com", ids["olga"])
    client.post(f"/admin/namespaces/{ids['sales']}/members", json={"user_id": ids["olga"], "permission": "read"},
                headers=_bearer(root))
    uid = _new_user("new@example.com")
    with psycopg.connect(DSN) as conn:
        assert spaces.apply_invites(conn, uid, "new@example.com") == 0
        assert identity.reaches(conn, uid, ids["sales"]) is None


def test_an_invitation_never_lowers_existing_access(box):
    client, _, root, ids = box
    _share(client, root, ids["sales"], ids["ivan"], "admin")
    _invite_row(ids, "ivan-home@example.com", ids["mark"], permission="read")
    with psycopg.connect(DSN) as conn:
        assert spaces.apply_invites(conn, ids["ivan"], "ivan-home@example.com") == 1
        assert identity.reaches(conn, ids["ivan"], ids["sales"]) == "admin"


# ─── asking to join ──────────────────────────────────────────────────────────
def test_asking_for_access_and_the_admin_approving_with_another_permission(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["olga"])
    assert client.get(f"/ui/api/spaces/{ids['sales']}/access").json() == {"status": None}
    r = client.post(f"/ui/api/spaces/{ids['sales']}/access", json={"permission": "write"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    assert client.get(f"/ui/api/spaces/{ids['sales']}/access").json()["status"] == "pending"

    h = _as(client, cfg, ids["mark"])
    sales = [s for s in client.get("/ui/api/spaces").json()["spaces"] if s["name"] == "sales"][0]
    assert sales["waiting"] == 1
    reqs = client.get(f"/ui/api/spaces/{ids['sales']}/members").json()["requests"]
    assert reqs[0]["person"]["id"] == ids["olga"] and reqs[0]["permission"] == "write"
    r = client.post(f"/ui/api/spaces/{ids['sales']}/requests/{reqs[0]['id']}",
                    json={"approve": True, "permission": "read", "expect_permission": "write"}, headers=h)
    assert r.status_code == 200 and r.json() == {"status": "approved", "permission": "read"}
    assert _members(client, ids["sales"])[ids["olga"]] == "read"


def test_asking_for_a_space_that_does_not_exist_looks_the_same(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["ivan"])
    real = client.post(f"/ui/api/spaces/{ids['hr']}/access", json={"permission": "read"}, headers=h)
    fake = client.post("/ui/api/spaces/7b0c3a1e-0000-4000-8000-000000000000/access",
                       json={"permission": "read"}, headers=h)
    assert real.status_code == fake.status_code == 200 and real.json() == fake.json()


def test_denied_requests_leave_no_member(box):
    client, cfg, _, ids = box
    client.post(f"/ui/api/spaces/{ids['sales']}/access", json={"permission": "read"},
                headers=_as(client, cfg, ids["olga"]))
    h = _as(client, cfg, ids["mark"])
    rid = client.get(f"/ui/api/spaces/{ids['sales']}/members").json()["requests"][0]["id"]
    assert client.post(f"/ui/api/spaces/{ids['sales']}/requests/{rid}", json={"approve": False},
                       headers=h).json() == {"status": "denied"}
    assert ids["olga"] not in _members(client, ids["sales"])


def test_someone_elses_space_request_cannot_be_decided(box):
    client, cfg, _, ids = box
    client.post(f"/ui/api/spaces/{ids['hr']}/access", json={"permission": "read"},
                headers=_as(client, cfg, ids["ivan"]))
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM access_request")
        rid = str(cur.fetchone()[0])
    h = _as(client, cfg, ids["mark"])
    for space in (ids["sales"], ids["hr"]):
        assert client.post(f"/ui/api/spaces/{space}/requests/{rid}", json={"approve": True},
                           headers=h).status_code == 404


# ─── profiles ────────────────────────────────────────────────────────────────
def test_your_own_profile_has_everything_and_counts_your_writes(box):
    client, cfg, _, ids = box
    _as(client, cfg, ids["mark"])
    got = client.get(f"/ui/api/people/{ids['mark']}").json()
    assert got["view"] == "self" and got["person"]["email"] == "mark@example.com"
    assert [s["name"] for s in got["spaces"]] == ["sales"]
    assert got["activity"]["total"] == 2 and got["activity"]["by_op"] == {"create": 2}
    assert got["activity"]["by_space"][0]["name"] == "sales"
    assert got["tokens"] and got["signins"] == []


def test_a_colleague_sees_who_you_are_and_only_what_you_wrote_together(box):
    client, cfg, root, ids = box
    _share(client, root, ids["sales"], ids["olga"], "read")
    _as(client, cfg, ids["mark"])
    got = client.get(f"/ui/api/people/{ids['olga']}").json()
    assert got["view"] == "colleague"
    assert "email" not in got["person"] and "tokens" not in got
    assert [s["name"] for s in got["shared_spaces"]] == ["sales"]
    assert got["activity"]["total"] == 0          # her hr writes are not mark's to see


def test_a_stranger_is_not_found(box):
    client, cfg, _, ids = box
    _as(client, cfg, ids["ivan"])
    assert client.get(f"/ui/api/people/{ids['mark']}").status_code == 404
    assert client.get("/ui/api/people/not-a-uuid").status_code == 404


def test_an_administrator_sees_anyones_profile(box):
    client, cfg, _, ids = box
    _as(client, cfg, ids["mgr"])
    got = client.get(f"/ui/api/people/{ids['olga']}").json()
    # the account is theirs to look after; what she wrote in spaces the manager
    # cannot open is not
    assert got["view"] == "admin" and got["can"]["manage"]
    assert got["activity"]["total"] == 0 and got["recent"] == []
    boss = client.get(f"/ui/api/people/{ids['boss']}").json()
    # a user manager does not get to look at an administrator's credentials
    assert boss["tokens"] is None and boss["signins"] is None and not boss["can"]["manage"]


def test_the_history_of_a_record_names_its_author(box):
    client, cfg, _, ids = box
    _as(client, cfg, ids["mark"])
    graph = client.get(f"/ui/api/spaces/{ids['sales']}/graph").json()
    rid = graph["records"][0]["id"]
    hist = client.get(f"/ui/api/spaces/{ids['sales']}/records/{rid}/history").json()["history"]
    assert hist[0]["author_id"] == ids["mark"] and hist[0]["op"] == "create"
    _as(client, cfg, ids["ivan"])
    assert client.get(f"/ui/api/spaces/{ids['sales']}/records/{rid}/history").status_code == 404


# ─── the directory and the switches ──────────────────────────────────────────
def test_only_administrators_search_people(box):
    client, cfg, _, ids = box
    _as(client, cfg, ids["mark"])
    assert client.get("/ui/api/admin/people?q=olga").status_code == 403
    _as(client, cfg, ids["mgr"])
    got = client.get("/ui/api/admin/people?q=petrova").json()
    assert [p["id"] for p in got["people"]] == [ids["olga"]] and got["total"] == 1
    assert client.get("/ui/api/admin/people?q=%25").json()["total"] == 0     # a % is a character, not a wildcard


def test_a_user_manager_switches_off_a_user_but_not_a_superadmin(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mgr"])
    assert client.post(f"/ui/api/admin/people/{ids['olga']}/disabled", json={"disabled": True},
                       headers=h).status_code == 200
    assert client.post(f"/ui/api/admin/people/{ids['boss']}/disabled", json={"disabled": True},
                       headers=h).status_code == 403
    assert client.post(f"/ui/api/admin/people/{ids['olga']}/role", json={"role": "user_manager"},
                       headers=h).status_code == 403


def test_a_superadmin_sets_roles_but_not_away_the_last_superadmin(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["boss"])
    assert client.post(f"/ui/api/admin/people/{ids['olga']}/role", json={"role": "user_manager"},
                       headers=h).status_code == 200
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE app_user SET disabled_at = now() WHERE role = 'superadmin' AND id <> %s",
                    (ids["boss"],))
    r = client.post(f"/ui/api/admin/people/{ids['boss']}/role", json={"role": "user"}, headers=h)
    assert r.status_code == 409


def test_an_administrator_revokes_tokens_and_ends_sessions(box):
    client, cfg, _, ids = box
    olga_session = _as(client, cfg, ids["olga"])
    assert olga_session
    olga_cookie = client.cookies.get(COOKIE)
    h = _as(client, cfg, ids["mgr"])
    tok = client.get(f"/ui/api/people/{ids['olga']}").json()["tokens"][0]["id"]
    assert client.delete(f"/ui/api/admin/people/{ids['olga']}/tokens/{tok}", headers=h).status_code == 200
    assert client.get("/memories", headers=_bearer(ids["tokens"]["olga"])).status_code == 401
    # another person's token id under this person is not found
    mark_tok = client.get(f"/ui/api/people/{ids['mark']}").json()["tokens"][0]["id"]
    assert client.delete(f"/ui/api/admin/people/{ids['olga']}/tokens/{mark_tok}", headers=h).status_code == 404
    assert client.post(f"/ui/api/admin/people/{ids['olga']}/sessions/end", headers=h).json()["ended"] >= 1
    client.cookies.clear()
    client.cookies.set(COOKIE, olga_cookie)
    assert client.get("/ui/api/session").status_code == 401


def test_an_administrator_can_take_back_even_the_last_sign_in_method(box):
    client, cfg, _, ids = box
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_user_identity (issuer, subject, user_id, provider) "
                    "VALUES ('https://id', 's1', %s, 'corp') RETURNING id", (ids["olga"],))
        iid = str(cur.fetchone()[0])
    h = _as(client, cfg, ids["mgr"])
    assert client.delete(f"/ui/api/admin/people/{ids['olga']}/signins/{iid}", headers=h).status_code == 200
    assert client.get(f"/ui/api/people/{ids['olga']}").json()["signins"] == []


def test_editing_a_profile_refuses_an_email_someone_else_has(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mgr"])
    url = f"/ui/api/admin/people/{ids['olga']}"
    assert client.patch(url, json={"email": "mark@example.com"}, headers=h).status_code == 409
    assert client.patch(url, json={"department": "HR", "email": "olga.p@example.com"}, headers=h).status_code == 200
    person = client.get(url.replace("/admin", "")).json()["person"]
    assert person["department"] == "HR" and person["email"] == "olga.p@example.com"


# ─── found in review ─────────────────────────────────────────────────────────
def test_adding_someone_again_never_lowers_their_access(box):
    client, cfg, root, ids = box
    _share(client, root, ids["sales"], ids["ivan"], "admin")
    h = _as(client, cfg, ids["mark"])
    client.post(f"/ui/api/spaces/{ids['sales']}/members", json={"email": "ivan@example.com", "permission": "read"},
                headers=h)
    assert _members(client, ids["sales"])[ids["ivan"]] == "admin"


def test_approving_an_old_request_never_lowers_access_given_since(box):
    client, cfg, _, ids = box
    client.post(f"/ui/api/spaces/{ids['sales']}/access", json={"permission": "read"},
                headers=_as(client, cfg, ids["olga"]))
    h = _as(client, cfg, ids["mark"])
    rid = client.get(f"/ui/api/spaces/{ids['sales']}/members").json()["requests"][0]["id"]
    client.post(f"/ui/api/spaces/{ids['sales']}/members", json={"email": "olga@example.com", "permission": "admin"},
                headers=h)
    # she is in now: no longer listed as waiting, and approving the stale request changes nothing
    assert client.get(f"/ui/api/spaces/{ids['sales']}/members").json()["requests"] == []
    r = client.post(f"/ui/api/spaces/{ids['sales']}/requests/{rid}",
                    json={"approve": True, "expect_permission": "read"}, headers=h)
    assert r.status_code == 200 and r.json()["permission"] == "admin"
    assert _members(client, ids["sales"])[ids["olga"]] == "admin"


def test_a_full_space_refuses_known_and_unknown_addresses_alike(box, monkeypatch):
    client, cfg, _, ids = box
    monkeypatch.setattr(spaces, "MAX_OPEN_INVITES", 1)
    h = _as(client, cfg, ids["mark"])
    url = f"/ui/api/spaces/{ids['sales']}/members"
    assert client.post(url, json={"email": "p0@example.com"}, headers=h).status_code == 200
    assert client.post(url, json={"email": "ivan@example.com"}, headers=h).status_code == 409
    assert client.post(url, json={"email": "p9@example.com"}, headers=h).status_code == 409


def test_adding_people_is_rate_limited(box, monkeypatch):
    client, cfg, _, ids = box
    monkeypatch.setattr(spaces, "ADDS_PER_HOUR", 2)
    # the limit is read when the routes are mounted: rebuild the app
    from memgres.server import create_app as mk
    with TestClient(mk(cfg), base_url=ORIGIN) as c2:
        h = _as(c2, cfg, ids["mark"])
        url = f"/ui/api/spaces/{ids['sales']}/members"
        assert [c2.post(url, json={"email": f"x{n}@example.com"}, headers=h).status_code
                for n in range(3)] == [200, 200, 429]


def test_a_session_with_no_account_cannot_leave_invitations_that_never_apply(box):
    """The env break-glass root owns no account, and an invitation carries its
    sender's authority: it would be stored and never apply."""
    _, _, _, ids = box
    root = identity.Principal(user_id=None, permission="admin", scope_namespace_id=None,
                              is_admin=True, role="superadmin")
    with psycopg.connect(DSN) as conn:
        with pytest.raises(spaces.Refused) as e:
            spaces.invite(conn, root, ids["sales"], email="new@example.com", permission="read", inviter_id=None)
        assert e.value.status == 409

def test_a_lapsed_invitation_is_marked_and_not_offered_to_the_queue(box):
    client, cfg, root, ids = box
    _share(client, root, ids["sales"], ids["olga"], "admin")
    _invite_row(ids, "new@example.com", ids["olga"])
    _share(client, root, ids["sales"], ids["olga"], "read")
    _as(client, cfg, ids["mark"])
    assert client.get(f"/ui/api/spaces/{ids['sales']}/members").json()["invites"][0]["lapsed"] is True
    with psycopg.connect(DSN) as conn:
        assert spaces.invites_for(conn, "new@example.com") == []


def test_a_colleague_is_not_told_anyones_role(box):
    client, cfg, root, ids = box
    _share(client, root, ids["sales"], ids["boss"], "read")
    _as(client, cfg, ids["mark"])
    assert "role" not in client.get(f"/ui/api/people/{ids['boss']}").json()["person"]


def test_the_own_account_guard_is_not_fooled_by_letter_case(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["boss"])
    r = client.post(f"/ui/api/admin/people/{ids['boss'].upper()}/disabled", json={"disabled": True}, headers=h)
    assert r.status_code == 409


def test_profile_fields_must_be_text(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mgr"])
    assert client.patch(f"/ui/api/admin/people/{ids['olga']}", json={"full_name": 5}, headers=h).status_code == 422


# ─── recent edits, shown only where the viewer can read ──────────────────────
def test_recent_edits_follow_what_the_viewer_can_read(box):
    client, cfg, root, ids = box
    _share(client, root, ids["sales"], ids["olga"], "read")
    # olga writes in her own hr and — via a token — nowhere else; mark writes in sales
    _as(client, cfg, ids["olga"])
    mine = client.get(f"/ui/api/people/{ids['olga']}").json()
    assert [r["path"] for r in mine["recent"]] == ["policies.vacation"]
    # mark, a colleague through sales, sees none of her hr writes
    _as(client, cfg, ids["mark"])
    assert client.get(f"/ui/api/people/{ids['olga']}").json()["recent"] == []
    # olga looking at mark sees his sales writes, newest first
    _as(client, cfg, ids["olga"])
    got = client.get(f"/ui/api/people/{ids['mark']}").json()["recent"]
    assert {r["path"] for r in got} == {"leads.qualify", "deals.stages"} and got[0]["space"] == "sales"
    # a superadmin reads every space, so sees everything
    _as(client, cfg, ids["boss"])
    assert len(client.get(f"/ui/api/people/{ids['mark']}").json()["recent"]) == 2
    assert client.get(f"/ui/api/people/{ids['olga']}").json()["activity"]["total"] == 1


# ─── administrators provision people ─────────────────────────────────────────
def test_an_administrator_creates_a_person(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mgr"])
    r = client.post("/ui/api/admin/people", json={"full_name": "Pavel Orlov", "email": "Pavel@Example.com",
                                                  "department": "Service"}, headers=h)
    assert r.status_code == 201, r.text
    got = client.get(f"/ui/api/people/{r.json()['id']}").json()["person"]
    assert got["email"] == "Pavel@Example.com" and got["role"] == "user" and got["department"] == "Service"
    assert client.post("/ui/api/admin/people", json={"email": "mark@example.com"}, headers=h).status_code == 409
    assert client.post("/ui/api/admin/people", json={}, headers=h).status_code == 422
    _as(client, cfg, ids["mark"])
    assert client.post("/ui/api/admin/people", json={"email": "x@example.com"},
                       headers=_as(client, cfg, ids["mark"])).status_code == 403


def test_the_right_to_create_spaces_is_a_switch(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mgr"])
    url = f"/ui/api/admin/people/{ids['olga']}/can-create-spaces"
    assert client.post(url, json={"allowed": True}, headers=h).status_code == 200
    assert client.get(f"/ui/api/people/{ids['olga']}").json()["person"]["can_create_namespace"] is True
    assert client.post(url, json={"allowed": False}, headers=h).status_code == 200
    assert client.get(f"/ui/api/people/{ids['olga']}").json()["person"]["can_create_namespace"] is False
    # not on an administrator's account
    assert client.post(f"/ui/api/admin/people/{ids['boss']}/can-create-spaces", json={"allowed": True},
                       headers=h).status_code == 403


def test_an_administrator_issues_a_token_for_someone(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mgr"])
    url = f"/ui/api/admin/people/{ids['olga']}/tokens"
    r = client.post(url, json={"label": "olga laptop", "permission": "read", "namespace_id": ids["hr"],
                               "expires_days": 30}, headers=h)
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    assert client.get("/memories", params={"space": "hr"}, headers=_bearer(tok)).status_code == 200
    assert client.post("/memories", json={"space": "hr", "path": "x.y", "body": "no"},
                       headers=_bearer(tok)).status_code in (401, 403)
    # the panel's own shape: never admin, always expiring, only a space she reaches
    assert client.post(url, json={"permission": "admin", "expires_days": 30}, headers=h).status_code == 422
    assert client.post(url, json={"permission": "read", "expires_days": 7}, headers=h).status_code == 422
    assert client.post(url, json={"permission": "read", "expires_days": 30, "namespace_id": ids["sales"]},
                       headers=h).status_code == 422
    # and a user manager cannot mint one for an administrator
    assert client.post(f"/ui/api/admin/people/{ids['boss']}/tokens",
                       json={"permission": "read", "expires_days": 30}, headers=h).status_code == 403


# ─── a space of one's own ────────────────────────────────────────────────────
def test_only_someone_granted_the_right_makes_a_space(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["ivan"])
    assert client.get("/ui/api/session").json()["can"]["create_space"] is False
    assert client.post("/ui/api/spaces", json={"name": "ivans"}, headers=h).status_code == 403

    admin_h = _as(client, cfg, ids["mgr"])
    client.post(f"/ui/api/admin/people/{ids['ivan']}/can-create-spaces", json={"allowed": True}, headers=admin_h)

    h = _as(client, cfg, ids["ivan"])
    assert client.get("/ui/api/session").json()["can"]["create_space"] is True
    r = client.post("/ui/api/spaces", json={"name": "ivans", "description": "mine"}, headers=h)
    assert r.status_code == 201, r.text
    got = client.get("/ui/api/spaces").json()["spaces"]
    assert [(s["name"], s["permission"], s["mine"]) for s in got] == [("ivans", "admin", True)]
    # the panel still only reads memory: the new space is empty and stays that way here
    assert client.get(f"/ui/api/spaces/{r.json()['id']}/graph").json()["total"] == 0
    assert client.post("/ui/api/spaces", json={"name": "ivans"}, headers=h).status_code == 409   # same name
    assert client.post("/ui/api/spaces", json={"name": "  "}, headers=h).status_code == 422


def test_an_administrator_always_may_make_a_space(box):
    client, cfg, _, ids = box
    h = _as(client, cfg, ids["mgr"])
    assert client.get("/ui/api/session").json()["can"]["create_space"] is True
    assert client.post("/ui/api/spaces", json={"name": "handbook"}, headers=h).status_code == 201
