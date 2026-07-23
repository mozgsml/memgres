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
    assert any("there" in h["body"] for h in hits)

    # move
    r = client.post(f"/memories/{mid}/move", json={"path": "moved.here"})
    assert r.status_code == 200 and r.json()["path"] == "moved.here"

    # forget
    assert client.delete(f"/memories/{mid}").status_code == 204
    assert client.get(f"/memories/{mid}").status_code == 404


def test_recall_tag_and_subtree_filters(client):
    client.post("/memories", json={"body": "apple pie recipe\n", "tags": ["food"],
                                   "path": "recipes.apple"})
    client.post("/memories", json={"body": "apple stock ticker\n", "tags": ["finance"],
                                   "path": "markets.apple"})
    # tag filter
    hits = client.get("/recall", params={"q": "apple", "tags": "finance"}).json()
    assert len(hits) == 1 and "ticker" in hits[0]["body"]
    # subtree filter
    hits = client.get("/recall", params={"q": "apple", "path_prefix": "recipes"}).json()
    assert len(hits) == 1 and "recipe" in hits[0]["body"]


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


def test_namespace_token_required(monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMGRES_DATABASE_URL", DSN)
    monkeypatch.setenv("MEMGRES_NAMESPACES", "true")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    monkeypatch.setenv("MEMGRES_FTS_LANGUAGE", "simple")
    app = create_app(load())
    with TestClient(app) as client:
        # no token -> 401
        assert client.post("/memories", json={"body": "x\n"}).status_code == 401
        # with token -> ok, and another token can't read it
        r = client.post("/memories", json={"body": "alice\n"},
                        headers={"Authorization": "Bearer alice-tok"})
        assert r.status_code == 201
        mid = r.json()["id"]
        assert client.get(f"/memories/{mid}",
                          headers={"X-Memgres-Token": "bob-tok"}).status_code == 404
        assert client.get(f"/memories/{mid}",
                          headers={"X-Memgres-Token": "alice-tok"}).status_code == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
