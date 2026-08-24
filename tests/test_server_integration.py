"""HTTP layer against a live Postgres via FastAPI TestClient."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("psycopg_pool")

from fastapi.testclient import TestClient  # noqa: E402

from memgres.config import load  # noqa: E402
from memgres.diffing import make_diff  # noqa: E402
from memgres.server import create_app  # noqa: E402

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
def client(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    app = create_app(load())
    with TestClient(app) as c:
        yield c


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_crud_flow(client):
    r = client.post("/memories", json={"body": "hello world\n", "tags": ["greet"],
                                       "path": "root.greeting", "source": "test"})
    assert r.status_code == 201
    m = r.json()
    mid, h0 = m["id"], m["content_hash"]

    # read
    assert client.get(f"/memories/{mid}").json()["body"] == "hello world\n"

    # diff edit with correct base hash
    d = make_diff("hello world\n", "hello there\n")
    r = client.patch(f"/memories/{mid}", json={"diff": d, "base_hash": h0})
    assert r.status_code == 200 and r.json()["body"] == "hello there\n"

    # stale diff -> 409
    r = client.patch(f"/memories/{mid}", json={"diff": d, "base_hash": h0})
    assert r.status_code == 409

    # history has create + diff
    hist = client.get(f"/memories/{mid}/history").json()
    assert [h["op"] for h in hist] == ["create", "diff"]

    # recall (lexical, auto)
    hits = client.get("/recall", params={"q": "there"}).json()
    assert any("there" in h["snippet"] for h in hits)   # short body → snippet==body

    # move
    r = client.post(f"/memories/{mid}/move", json={"path": "moved.here"})
    assert r.status_code == 200 and r.json()["path"] == "moved.here"

    # forget
    assert client.delete(f"/memories/{mid}").status_code == 204
    assert client.get(f"/memories/{mid}").status_code == 404


def test_replace_edit_over_http(client):
    r = client.post("/memories", json={"body": "alpha\nbeta\ngamma\n"})
    mid = r.json()["id"]
    # substring replace: server finds & rewrites
    r = client.patch(f"/memories/{mid}",
                     json={"replace_old": "beta", "replace_new": "BETA"})
    assert r.status_code == 200 and r.json()["body"] == "alpha\nBETA\ngamma\n"
    # not found -> 422
    r = client.patch(f"/memories/{mid}",
                     json={"replace_old": "zzz", "replace_new": "x"})
    assert r.status_code == 422
    # ambiguous -> 422; with replace_all -> ok (on a dedicated record)
    r2 = client.post("/memories", json={"body": "dup dup\n"})
    mid2 = r2.json()["id"]
    amb = client.patch(f"/memories/{mid2}", json={"replace_old": "dup", "replace_new": "z"})
    assert amb.status_code == 422
    ok = client.patch(f"/memories/{mid2}",
                      json={"replace_old": "dup", "replace_new": "z", "replace_all": True})
    assert ok.status_code == 200 and ok.json()["body"] == "z z\n"


def test_replace_missing_new_errors_not_silent_delete(client):
    # Regression (meta.memgres.replace_new_dropped): a lone replace_old with
    # replace_new omitted must be REJECTED, not coerced to ("old", "") and
    # silently delete the matched text on a 200.
    r = client.post("/memories", json={"body": "keep\nanchor\ntail\n"})
    mid, h0 = r.json()["id"], r.json()["content_hash"]
    r = client.patch(f"/memories/{mid}", json={"replace_old": "anchor"})
    assert r.status_code == 422
    got = client.get(f"/memories/{mid}").json()          # body untouched, no seq bump
    assert got["body"] == "keep\nanchor\ntail\n" and got["content_hash"] == h0
    # a lone replace_new is likewise rejected
    assert client.patch(f"/memories/{mid}",
                        json={"replace_new": "x"}).status_code == 422
    # an EXPLICIT empty replace_new is a deliberate deletion (still allowed)
    r = client.patch(f"/memories/{mid}",
                     json={"replace_old": "anchor\n", "replace_new": ""})
    assert r.status_code == 200 and r.json()["body"] == "keep\ntail\n"


def test_recall_tag_and_subtree_filters(client):
    client.post("/memories", json={"body": "apple pie recipe\n", "tags": ["food"],
                                   "path": "recipes.apple"})
    client.post("/memories", json={"body": "apple stock ticker\n", "tags": ["finance"],
                                   "path": "markets.apple"})
    # tag filter
    hits = client.get("/recall", params={"q": "apple", "tags": "finance"}).json()
    assert len(hits) == 1 and "ticker" in hits[0]["snippet"]
    # subtree filter
    hits = client.get("/recall", params={"q": "apple", "path_prefix": "recipes"}).json()
    assert len(hits) == 1 and "recipe" in hits[0]["snippet"]


def test_list_memories_route(client):
    client.post("/memories", json={"body": "gamma line\n", "path": "decisions.c"})
    client.post("/memories", json={"body": "alpha line\nmore\n", "path": "decisions.a",
                                    "tags": ["keep"]})
    client.post("/memories", json={"body": "beta line\n", "path": "decisions.b"})
    client.post("/memories", json={"body": "other\n", "path": "ops.x"})

    rows = client.get("/memories", params={"path": "decisions"}).json()
    assert [r["path"] for r in rows] == ["decisions.a", "decisions.b", "decisions.c"]
    assert rows[0]["preview"] == "alpha line"      # first line only
    # tag filter narrows
    only = client.get("/memories", params={"path": "decisions", "tags": "keep"}).json()
    assert [r["path"] for r in only] == ["decisions.a"]
    # pagination
    page = client.get("/memories", params={"path": "decisions", "limit": 1,
                                           "offset": 1}).json()
    assert [r["path"] for r in page] == ["decisions.b"]


def test_info_route(client):
    info = client.get("/info").json()
    assert set(info) == {"version", "schema_version", "limits", "embed",
                         "recall_modes", "vector_backend", "key_mode", "fts_language"}
    assert isinstance(info["version"], str) and info["version"]
    assert info["recall_modes"] == ["lexical"]     # embed provider none in fixture
    assert info["key_mode"] == "single"
    assert "database_url" not in info and "token" not in info


def test_blame_lines_query(client):
    r = client.post("/memories", json={"body": "a\nb\nc\nd\ne\n", "source": "x"})
    mid = r.json()["id"]
    # single line
    got = client.get(f"/memories/{mid}/blame", params={"lines": "2"}).json()
    assert [g["line"] for g in got] == [2]
    # range + list
    got = client.get(f"/memories/{mid}/blame", params={"lines": "1,3-4"}).json()
    assert [g["line"] for g in got] == [1, 3, 4]
    # default = grouped: one author -> one block spanning 1..5
    grouped = client.get(f"/memories/{mid}/blame").json()
    assert len(grouped) == 1 and grouped[0]["start"] == 1 and grouped[0]["end"] == 5
    # group=false -> per-line, whole doc
    assert len(client.get(f"/memories/{mid}/blame", params={"group": "false"}).json()) == 5
    # text=false -> ownership map, no body
    assert "text" not in client.get(
        f"/memories/{mid}/blame", params={"text": "false"}).json()[0]


def test_identity_open_mode_over_http(monkeypatch):
    from memgres import identity
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "open")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    app = create_app(load())
    alice, bob = identity.new_token(), identity.new_token()
    with TestClient(app) as client:
        # no token -> 401
        assert client.post("/memories", json={"body": "x\n"}).status_code == 401
        ha = {"Authorization": f"Bearer {alice}"}
        hb = {"Authorization": f"Bearer {bob}"}
        # naming a space that does not exist is an error, not a new space
        assert client.post("/memories",
                           json={"body": "alice secret\n", "space": "vault"},
                           headers=ha).status_code == 404
        # so alice asks for it, then writes
        assert client.post("/spaces", json={"name": "vault"},
                           headers=ha).status_code == 201
        r = client.post("/memories", json={"body": "alice secret\n", "space": "vault"},
                        headers=ha)
        assert r.status_code == 201
        mid = r.json()["id"]
        # bob can't read alice's memory
        assert client.get(f"/memories/{mid}", headers=hb).status_code == 404
        # alice reads it back by naming her space
        assert client.get(f"/memories/{mid}", params={"space": "vault"},
                          headers=ha).json()["body"] == "alice secret\n"
        # /spaces lists alice's vault
        spaces = client.get("/spaces", headers=ha).json()
        assert [s["name"] for s in spaces] == ["vault"]
        # bob makes his own space and writes there
        assert client.post("/spaces", json={"name": "bobs"},
                           headers=hb).status_code == 201
        assert client.post("/memories", json={"body": "bob note\n"},
                           headers=hb).status_code == 201
        # recall is scoped: bob sees his note but never alice's secret
        assert client.get("/recall", params={"q": "note"}, headers=hb).json()
        assert client.get("/recall", params={"q": "secret"}, headers=hb).json() == []


def test_admin_provisioning_and_request_access(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "managed")
    monkeypatch.setenv("MEMGRES_ADMIN_TOKEN", "root-admin")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    app = create_app(load())
    A = {"Authorization": "Bearer root-admin"}
    with TestClient(app) as client:
        # non-admin can't provision
        assert client.post("/admin/users", json={"name": "x"}).status_code == 403
        # admin creates two users, a namespace, and an admin token for the owner
        owner = client.post("/admin/users", json={"name": "owner"}, headers=A).json()["id"]
        joiner = client.post("/admin/users", json={"name": "joiner"}, headers=A).json()["id"]
        ns = client.post("/admin/namespaces",
                         json={"owner_user_id": owner, "name": "team"},
                         headers=A).json()["id"]
        owner_tok = client.post("/admin/tokens",
                                json={"user_id": owner, "permission": "admin"},
                                headers=A).json()["token"]
        joiner_tok = client.post("/admin/tokens",
                                 json={"user_id": joiner, "permission": "write"},
                                 headers=A).json()["token"]
        ho = {"Authorization": f"Bearer {owner_tok}"}
        hj = {"Authorization": f"Bearer {joiner_tok}"}

        # owner writes into the team namespace
        r = client.post("/memories", json={"body": "team memo\n", "space": "team"},
                        headers=ho)
        assert r.status_code == 201
        mid = r.json()["id"]
        # joiner can't reach it yet
        assert client.get(f"/memories/{mid}", params={"space_id": ns},
                          headers=hj).status_code == 404
        # joiner requests read access; owner approves. The requester's receipt
        # says only that the request was submitted — no id, and nothing that
        # distinguishes an unreachable namespace from one that doesn't exist.
        r = client.post(f"/spaces/{ns}/access-requests", json={"permission": "read"},
                        headers=hj)
        assert r.status_code == 202 and r.json() == {"status": "submitted"}
        pending = client.get(f"/spaces/{ns}/access-requests", headers=ho).json()
        assert len(pending) == 1
        rid = pending[0]["id"]                  # the id lives with the decider
        # joiner (only a requester, no membership) can't approve their own request
        # (404 — the namespace isn't even visible to them, existence not leaked)
        assert client.post(f"/access-requests/{rid}/approve",
                           headers=hj).status_code in (403, 404)
        assert client.post(f"/access-requests/{rid}/approve",
                           headers=ho).status_code == 200
        # now joiner reads it (by id — it's a shared space)
        assert client.get(f"/memories/{mid}", params={"space_id": ns},
                          headers=hj).json()["body"] == "team memo\n"
        # but joiner's read is capped at read: can't edit
        assert client.patch(f"/memories/{mid}",
                            json={"body": "hacked\n", "space_id": ns},
                            headers=hj).status_code == 401
        # a malformed (non-uuid) space_id is a 400, not a 500 / aborted tx
        assert client.get("/recall", params={"q": "x", "space_id": "not-a-uuid"},
                          headers=ho).status_code == 400


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_recall_over_several_namespaces(monkeypatch):
    """The wire shape of a multi-namespace address: repeated query params
    (`?space=a&space=b`), a single `?space=all`, and the hit telling you which
    namespace answered. A bare `?space=a` must keep meaning exactly one space —
    the list form is additive, not a break."""
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "open")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    from memgres import identity

    app = create_app(load())
    tok = identity.new_token()
    h = {"Authorization": f"Bearer {tok}"}
    with TestClient(app) as client:
        for space in ("work", "home", "spare"):
            assert client.post("/spaces", json={"name": space},
                               headers=h).status_code == 201
            assert client.post("/memories",
                               json={"body": f"apple in {space}\n", "space": space},
                               headers=h).status_code == 201

        # several reachable and none named → refused, with the candidates
        r = client.get("/recall", params={"q": "apple"}, headers=h)
        assert r.status_code == 422 and "work" in r.text and "home" in r.text

        # repeated param = exactly those two
        r = client.get("/recall", params={"q": "apple", "space": ["work", "home"]},
                       headers=h)
        assert {hit["space"] for hit in r.json()} == {"work", "home"}

        # the keyword takes them all
        r = client.get("/recall", params={"q": "apple", "space": "all"}, headers=h)
        assert {hit["space"] for hit in r.json()} == {"work", "home", "spare"}
        assert all(hit["space_id"] for hit in r.json())

        # one name is still one namespace
        r = client.get("/recall", params={"q": "apple", "space": "work"}, headers=h)
        assert [hit["space"] for hit in r.json()] == ["work"]

        # browse and title-find address the same way
        assert len(client.get("/memories", params={"space": "all"},
                              headers=h).json()) == 3
        assert client.get("/find", params={"q": "apple", "space": "all"},
                          headers=h).status_code == 200


def test_a_memory_is_addressable_by_path_over_http(client):
    """The URL segment takes either address, told apart by whether it parses
    as a uuid."""
    r = client.post("/memories", json={"body": "one\n", "path": "ops.postgres"})
    assert r.status_code == 201 and r.json()["created"] is True
    mid = r.json()["id"]

    assert client.get("/memories/ops.postgres").json()["id"] == mid
    assert client.get(f"/memories/{mid}").json()["id"] == mid
    assert client.get("/memories/ops.postgres/history").json()[0]["op"] == "create"

    r = client.patch("/memories/ops.postgres", json={"body": "two\n"})
    assert r.status_code == 200 and r.json()["created"] is False
    assert client.get(f"/memories/{mid}").json()["body"] == "two\n"


def test_a_stale_path_is_a_conflict_not_a_second_memory(client):
    """The whole point, over the wire: a write to an address a memory left is
    refused with where it went — not answered with a quiet duplicate."""
    mid = client.post("/memories", json={"body": "real\n",
                                         "path": "ops.old"}).json()["id"]
    assert client.post(f"/memories/{mid}/move",
                       json={"path": "ops.new"}).status_code == 200

    r = client.post("/memories", json={"body": "dupe\n", "path": "ops.old"})
    assert r.status_code == 409 and "ops.new" in r.text
    assert len(client.get("/memories").json()) == 1        # nothing was created

    # a read, by contrast, follows and says the address changed
    got = client.get("/memories/ops.old").json()
    assert got["id"] == mid and got["moved_from"] == "ops.old"
    assert client.get("/memories/ops.old",
                      params={"if_moved": "error"}).status_code == 409

    # and the two deliberate answers both work
    r = client.patch("/memories/ops.old", json={"body": "edited\n",
                                                "if_moved": "follow"})
    assert r.status_code == 200 and r.json()["id"] == mid
    r = client.post("/memories", json={"body": "new tenant\n", "path": "ops.old",
                                       "if_moved": "create"})
    assert r.status_code == 201 and r.json()["id"] != mid


def test_creating_at_an_occupied_path_is_a_conflict(client):
    mid = client.post("/memories", json={"body": "mine\n",
                                         "path": "ops.a"}).json()["id"]
    r = client.post("/memories", json={"body": "also\n", "path": "ops.a"})
    assert r.status_code == 409 and mid in r.text
    assert client.get(f"/memories/{mid}").json()["body"] == "mine\n"


def test_a_hyphenated_path_is_still_a_path(client):
    """ltree labels accept hyphens (and non-ASCII) on modern Postgres, so the
    URL segment cannot be classified by "looks like it has a dash" — it is
    classified by whether it parses as a uuid."""
    mid = client.post("/memories", json={"body": "rate limits\n",
                                         "path": "ops.rate-limits"}).json()["id"]
    assert client.get("/memories/ops.rate-limits").json()["id"] == mid
    assert client.patch("/memories/ops.rate-limits",
                        json={"body": "two\n"}).status_code == 200

    mid2 = client.post("/memories", json={"body": "unicode\n",
                                          "path": "ops.тариф"}).json()["id"]
    assert client.get("/memories/ops.тариф").json()["id"] == mid2


def test_creating_spaces_and_aliasing_them_over_http(monkeypatch):
    """The explicit door that replaced lazy creation, and the alias that settles
    a name two people both used."""
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_KEY_MODE", "open")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    from memgres import identity

    app = create_app(load())
    alice, bob = identity.new_token(), identity.new_token()
    with TestClient(app) as client:
        ha = {"Authorization": f"Bearer {alice}"}
        hb = {"Authorization": f"Bearer {bob}"}
        a_ns = client.post("/spaces", json={"name": "notes"},
                           headers=ha).json()["id"]
        b_ns = client.post("/spaces", json={"name": "notes"},
                           headers=hb).json()["id"]
        # each owns a 'notes'; unshared, the name is unambiguous for each
        assert client.post("/memories", json={"body": "mine\n", "space": "notes"},
                           headers=ha).status_code == 201

        # bob shares his with alice, and now the bare name means two things
        with psycopg.connect(DSN, autocommit=True) as conn:
            identity.add_member(conn, b_ns, _owner_of(a_ns), "read")
        r = client.get("/recall", params={"q": "mine", "space": "notes"}, headers=ha)
        assert r.status_code == 422 and a_ns in r.text and b_ns in r.text

        # an alias settles it, and grants nothing that wasn't already reachable
        assert client.post("/spaces/aliases",
                           json={"alias": "bobs", "space_id": b_ns},
                           headers=ha).status_code == 201
        assert client.get("/recall", params={"q": "anything", "space": "bobs"},
                          headers=ha).status_code == 200
        # the alias shows up as what to type for that space
        spaces = {s["id"]: s for s in client.get("/spaces", headers=ha).json()}
        assert spaces[b_ns]["alias"] == "bobs" and spaces[a_ns]["alias"] is None

        # dropping it puts the ambiguity back — the namespace itself is untouched
        assert client.delete("/spaces/aliases/bobs", headers=ha).status_code == 204
        assert client.get("/recall", params={"q": "mine", "space": "notes"},
                          headers=ha).status_code == 422


def _owner_of(namespace_id: str) -> str:
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT owner_user_id FROM namespace WHERE id=%s",
                    (namespace_id,))
        return str(cur.fetchone()[0])
