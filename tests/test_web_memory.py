"""The panel's read-only view of memory: spaces, the graph, a record, search —
and that none of it reaches past what the account may read."""

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
from memgres.web import memory, sessions  # noqa: E402

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
                 "MEMGRES_ADMIN_ROLE": "superadmin", "MEMGRES_WEB_ENABLED": "true", "MEMGRES_PUBLIC_URL": "https://testserver"}.items():
        monkeypatch.setenv(k, v)
    cfg = load()
    with TestClient(create_app(cfg), base_url=ORIGIN) as c:
        mark = c.post("/admin/users", json={"name": "mark"}, headers=_bearer(root)).json()["id"]
        olga = c.post("/admin/users", json={"name": "olga"}, headers=_bearer(root)).json()["id"]
        sales = c.post("/admin/namespaces", json={"owner_user_id": mark, "name": "sales"},
                       headers=_bearer(root)).json()["id"]
        hr = c.post("/admin/namespaces", json={"owner_user_id": olga, "name": "hr"},
                    headers=_bearer(root)).json()["id"]
        mark_tok = c.post("/admin/tokens", json={"user_id": mark}, headers=_bearer(root)).json()["token"]
        olga_tok = c.post("/admin/tokens", json={"user_id": olga}, headers=_bearer(root)).json()["token"]
        w = lambda tok, **kw: c.post("/memories", json=kw, headers=_bearer(tok))  # noqa: E731
        assert w(mark_tok, space="sales", path="leads.qualify", title="Qualifying a lead",
                 body="Budget, space, timeline. Then [[deals.stages]].").status_code == 201
        assert w(mark_tok, space="sales", path="deals.stages", title="Deal stages",
                 body="Five stages; a demo is stage three.").status_code == 201
        assert w(mark_tok, space="sales", path="deals.discounts", title="Discount approval",
                 body="Up to ten percent the rep. See [[deals.stages]].").status_code == 201
        assert w(olga_tok, space="hr", path="policies.vacation", title="Vacation requests",
                 body="Two weeks notice. Secret salary grid lives elsewhere.").status_code == 201
        yield c, cfg, {"mark": mark, "olga": olga, "sales": sales, "hr": hr}


def _as(client, cfg, uid):
    with psycopg.connect(DSN) as conn:
        sid, _ = sessions.create(conn, cfg, identity.Principal(
            user_id=uid, permission="read", scope_namespace_id=None), via="oidc:test")
    client.cookies.set(COOKIE, sid)


def test_spaces_list_what_the_account_reaches_with_counts(box):
    client, cfg, ids = box
    _as(client, cfg, ids["mark"])
    got = client.get("/ui/api/spaces").json()["spaces"]
    assert [(s["name"], s["records"]) for s in got] == [("sales", 3)]


def test_the_graph_is_the_whole_space_shape_without_bodies(box):
    client, cfg, ids = box
    _as(client, cfg, ids["mark"])
    g = client.get(f"/ui/api/spaces/{ids['sales']}/graph").json()
    assert g["total"] == 3 and not g["truncated"]
    by_path = {r["path"]: r["id"] for r in g["records"]}
    assert set(by_path) == {"leads.qualify", "deals.stages", "deals.discounts"}
    assert {(l["a"], l["b"]) for l in g["links"]} == {
        (by_path["leads.qualify"], by_path["deals.stages"]),
        (by_path["deals.discounts"], by_path["deals.stages"])}
    assert "body" not in g["records"][0] and "Budget" not in client.get(
        f"/ui/api/spaces/{ids['sales']}/graph").text


def test_someone_elses_space_reads_as_missing(box):
    client, cfg, ids = box
    _as(client, cfg, ids["mark"])
    theirs = client.get(f"/ui/api/spaces/{ids['hr']}/graph")
    made_up = client.get("/ui/api/spaces/11111111-1111-1111-1111-111111111111/graph")
    garbage = client.get("/ui/api/spaces/nope/graph")
    assert theirs.status_code == made_up.status_code == 404
    assert garbage.status_code in (404, 422)
    assert theirs.json() == made_up.json()
    assert client.get(f"/ui/api/spaces/{ids['hr']}/search", params={"q": "salary"}).status_code == 404


def test_a_record_with_its_links(box):
    client, cfg, ids = box
    _as(client, cfg, ids["mark"])
    g = client.get(f"/ui/api/spaces/{ids['sales']}/graph").json()
    stages = next(r["id"] for r in g["records"] if r["path"] == "deals.stages")
    r = client.get(f"/ui/api/spaces/{ids['sales']}/records/{stages}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["record"]["body"].startswith("Five stages")
    assert len(body["links"]["in"]) == 2


def test_a_record_from_another_space_is_not_found_through_mine(box):
    """Asking for hr's record under my own space id must not read it."""
    client, cfg, ids = box
    _as(client, cfg, ids["olga"])
    hr_rec = client.get(f"/ui/api/spaces/{ids['hr']}/graph").json()["records"][0]["id"]
    _as(client, cfg, ids["mark"])
    assert client.get(f"/ui/api/spaces/{ids['sales']}/records/{hr_rec}").status_code == 404


def test_search_within_a_space(box):
    client, cfg, ids = box
    _as(client, cfg, ids["mark"])
    hits = client.get(f"/ui/api/spaces/{ids['sales']}/search", params={"q": "stages"}).json()["hits"]
    assert hits and {h["path"] for h in hits} >= {"deals.stages"}
    assert client.get(f"/ui/api/spaces/{ids['sales']}/search", params={"q": "  "}).json()["hits"] == []


def test_the_graph_says_when_it_is_cut(box, monkeypatch):
    client, cfg, ids = box
    monkeypatch.setattr(memory, "MAX_GRAPH_NODES", 2)
    _as(client, cfg, ids["mark"])
    g = client.get(f"/ui/api/spaces/{ids['sales']}/graph").json()
    assert g["truncated"] and g["total"] == 3 and len(g["records"]) == 2


def test_memory_needs_a_session(box):
    client, _, ids = box
    assert client.get("/ui/api/spaces").status_code == 401
    assert client.get(f"/ui/api/spaces/{ids['sales']}/graph").status_code == 401


def test_a_scoped_member_reads_a_shared_space(box):
    client, cfg, ids = box
    root_client_headers = None  # noqa: F841
    with psycopg.connect(DSN) as conn:
        identity.add_member(conn, ids["hr"], ids["mark"], "read")
    _as(client, cfg, ids["mark"])
    names = {s["name"]: s["permission"] for s in client.get("/ui/api/spaces").json()["spaces"]}
    assert names == {"sales": "admin", "hr": "read"}
    assert client.get(f"/ui/api/spaces/{ids['hr']}/graph").json()["total"] == 1


# ─── a superadmin opens any space ────────────────────────────────────────────
def _superadmin(client, cfg):
    with psycopg.connect(DSN, autocommit=True) as conn:
        uid = identity.create_user(conn, name="boss", role="superadmin")
    _as(client, cfg, uid)
    return uid


def test_a_superadmin_lists_and_opens_any_space_it_is_not_in(box):
    client, cfg, ids = box
    _superadmin(client, cfg)
    assert client.get("/ui/api/spaces").json()["spaces"] == []          # its own: none
    listed = {s["name"]: s for s in client.get("/ui/api/admin/spaces").json()["spaces"]}
    assert set(listed) == {"sales", "hr"} and listed["sales"]["records"] == 3
    assert [s["name"] for s in client.get("/ui/api/admin/spaces?q=hr").json()["spaces"]] == ["hr"]
    meta = client.get(f"/ui/api/spaces/{ids['hr']}").json()
    assert meta["member"] is False and meta["name"] == "hr"
    assert client.get(f"/ui/api/spaces/{ids['hr']}/graph").json()["total"] == 1
    assert client.get(f"/ui/api/spaces/{ids['hr']}/search?q=vacation").json()["hits"]


def test_nobody_else_gets_the_list_or_a_space_they_are_not_in(box):
    client, cfg, ids = box
    _as(client, cfg, ids["mark"])
    assert client.get("/ui/api/admin/spaces").status_code == 403
    assert client.get(f"/ui/api/spaces/{ids['hr']}").status_code == 404
    assert client.get(f"/ui/api/spaces/{ids['sales']}").json()["member"] is True
