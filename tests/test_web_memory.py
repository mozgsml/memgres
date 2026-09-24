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


def test_a_superadmin_asking_for_a_space_that_does_not_exist_gets_not_found(box):
    client, cfg, _ = box
    _superadmin(client, cfg)
    assert client.get("/ui/api/spaces/7b0c3a1e-0000-4000-8000-000000000000").status_code == 404


def test_the_list_of_every_space_says_when_it_is_cut(box, monkeypatch):
    client, cfg, _ = box
    _superadmin(client, cfg)
    monkeypatch.setattr(memory, "MAX_EVERY_SPACE", 1)
    got = client.get("/ui/api/admin/spaces").json()
    assert len(got["spaces"]) == 1 and got["truncated"] is True


def test_the_panel_shows_who_wrote_which_lines(box):
    """Blame has existed in the core since the history wave; the panel simply
    never had a way in. Grouped into runs, because a per-line list of five
    hundred identically-attributed rows is not something a person reads.

    Ranges and provenance, no text: the panel already has the body and tints it
    where it stands, so what it cannot work out for itself is who wrote each
    line and what they said they were doing."""
    client, cfg, ids = box
    tok = client.post("/admin/tokens", json={"user_id": ids["mark"]},
                      headers=_bearer(os.environ["MEMGRES_ADMIN_TOKEN"])).json()["token"]
    made = client.post("/memories", json={"space": "sales", "path": "notes.blame",
                                          "title": "Notes", "body": "first line\nsecond line\n",
                                          "source": "the first meeting", "reason": "seed"},
                       headers=_bearer(tok))
    rid = made.json()["id"]
    edit = client.patch(f"/memories/{rid}", json={"title": "Notes",
                        "body": "first line\nsecond line\nthird line\n",
                        "source": "a later mail", "reason": "one more line",
                        "valid_at": "2026-09-01"},
                        headers=_bearer(tok))
    assert edit.status_code == 200, edit.text

    h = _as(client, cfg, ids["mark"])
    r = client.get(f"/ui/api/spaces/{ids['sales']}/records/{rid}/blame")
    assert r.status_code == 200, r.text
    blocks = r.json()["blame"]
    assert [(b["start"], b["end"]) for b in blocks] == [(1, 2), (3, 3)]
    assert all("text" not in b for b in blocks)      # the body is not sent twice
    last = blocks[-1]
    assert last["source"] == "a later mail" and last["reason"] == "one more line"
    assert last["valid_at"] == "2026-09-01" and last["author"]
    assert blocks[0]["source"] == "the first meeting"

    # someone with no way into that space is told nothing about it
    _as(client, cfg, ids["olga"])
    assert client.get(f"/ui/api/spaces/{ids['sales']}/records/{rid}/blame").status_code == 404


def test_a_person_picks_a_colour_for_a_space_or_a_branch(box):
    """The graph colours a branch by hashing its name — stable and meaningless.
    A choice is per person (like the interface language beside it), so nobody
    repaints what everyone else sees, and it survives to another device."""
    client, cfg, ids = box
    _as(client, cfg, ids["mark"])
    # a changing request carries the CSRF token and a same-origin Origin
    h = {"Origin": ORIGIN, "X-Memgres-CSRF": client.get("/ui/api/session").json()["csrf"]}
    assert client.get("/ui/api/session").json()["user"]["ui_colors"] == {}

    r = client.put("/ui/api/me/colors", json={"key": ids["sales"], "color": "amber"}, headers=h)
    assert r.status_code == 200 and r.json()["colors"] == {ids["sales"]: "amber"}
    branch = f"{ids['sales']}:leads"
    client.put("/ui/api/me/colors", json={"key": branch, "color": "lime"}, headers=h)

    # it comes back with the session, which is what the panel draws from
    colours = client.get("/ui/api/session").json()["user"]["ui_colors"]
    assert colours == {ids["sales"]: "amber", branch: "lime"}

    # a colour the panel cannot draw is refused rather than stored
    assert client.put("/ui/api/me/colors", json={"key": branch, "color": "octarine"},
                      headers=h).status_code == 422

    # the picker offers more than the eight the automatic colour hashes to,
    # and nothing BUT colours: "automatic" is the absence of an entry, so it is
    # sent as `color: null` and has no name of its own to store
    assert client.put("/ui/api/me/colors", json={"key": branch, "color": "teal"},
                      headers=h).status_code == 200
    for word in ("auto", "inherit"):
        assert client.put("/ui/api/me/colors", json={"key": branch, "color": word},
                          headers=h).status_code == 422

    # and clearing one brings the automatic colour back
    client.put("/ui/api/me/colors", json={"key": branch, "color": None}, headers=h)
    assert client.get("/ui/api/session").json()["user"]["ui_colors"] == {ids["sales"]: "amber"}

    # someone else's choices are their own
    _as(client, cfg, ids["olga"])
    assert client.get("/ui/api/session").json()["user"]["ui_colors"] == {}
