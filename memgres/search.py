"""Recall: lexical (Postgres FTS), semantic (pgvector), or hybrid (RRF).

All three share the same filters — namespace, tags (``@>`` contains-all), tree
subtree (``path <@ prefix``), and not-expired — so you can scope any query to a
branch of the tree or a set of tags. ``mode='auto'`` picks semantic when an
embedder is configured, else lexical.

Hybrid fuses the two ranked lists with Reciprocal Rank Fusion (RRF): exact
identifiers that dense vectors fumble come in via lexical, meaning-based matches
via vectors, and neither backend needs to know about the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

RRF_K = 60  # standard RRF damping constant


@dataclass
class Hit:
    id: str
    body: str
    tags: List[str]
    path: Optional[str]
    score: float


def _filters(ns: str, tags: Optional[Sequence[str]], path_prefix: Optional[str]):
    """Return (sql_fragment, params) for the shared WHERE tail."""
    sql = ["namespace = %s", "(expires_at IS NULL OR expires_at > now())"]
    params: list = [ns]
    if tags:
        sql.append("tags @> %s")           # row must contain all requested tags
        params.append(list(tags))
    if path_prefix:
        sql.append("path <@ %s::ltree")     # subtree of the prefix
        params.append(path_prefix)
    return " AND ".join(sql), params


def _lexical(conn, cfg, ns, query, k, tags, path_prefix) -> List[Hit]:
    where, params = _filters(ns, tags, path_prefix)
    sql = (
        "SELECT id, body, tags, path::text, "
        "ts_rank(fts, plainto_tsquery(%s::regconfig, %s)) AS score "
        f"FROM memory WHERE {where} "
        "AND fts @@ plainto_tsquery(%s::regconfig, %s) "
        "ORDER BY score DESC LIMIT %s"
    )
    args = [cfg.fts_language, query] + params + [cfg.fts_language, query, k]
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [Hit(str(r[0]), r[1], list(r[2]), r[3], float(r[4]))
                for r in cur.fetchall()]


def _semantic(conn, cfg, embedder, ns, query, k, tags, path_prefix) -> List[Hit]:
    if not embedder:
        raise RuntimeError("semantic recall needs an embedder (MEMGRES_EMBED_PROVIDER)")
    qv = "[" + ",".join(repr(float(x)) for x in embedder.embed_query(query)) + "]"
    where, params = _filters(ns, tags, path_prefix)
    sql = (
        "SELECT id, body, tags, path::text, "
        "1 - (embedding <=> %s::vector) AS score "
        f"FROM memory WHERE {where} AND embedding IS NOT NULL "
        "ORDER BY embedding <=> %s::vector ASC LIMIT %s"
    )
    args = [qv] + params + [qv, k]
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [Hit(str(r[0]), r[1], list(r[2]), r[3], float(r[4]))
                for r in cur.fetchall()]


def _rrf(lists: Sequence[List[Hit]], k: int) -> List[Hit]:
    scores: dict = {}
    keep: dict = {}
    for hits in lists:
        for rank, h in enumerate(hits):
            scores[h.id] = scores.get(h.id, 0.0) + 1.0 / (RRF_K + rank + 1)
            keep[h.id] = h
    fused = sorted(keep.values(), key=lambda h: scores[h.id], reverse=True)
    for h in fused:
        h.score = scores[h.id]
    return fused[:k]


def recall(conn, cfg, embedder, ns: str, query: str, *, k: int = 10,
           tags: Optional[Sequence[str]] = None, path_prefix: Optional[str] = None,
           mode: str = "auto") -> List[Hit]:
    if mode == "auto":
        mode = "semantic" if embedder else "lexical"
    if mode == "lexical":
        return _lexical(conn, cfg, ns, query, k, tags, path_prefix)
    if mode == "semantic":
        return _semantic(conn, cfg, embedder, ns, query, k, tags, path_prefix)
    if mode == "hybrid":
        lex = _lexical(conn, cfg, ns, query, k, tags, path_prefix)
        sem = _semantic(conn, cfg, embedder, ns, query, k, tags, path_prefix)
        return _rrf([sem, lex], k)
    raise ValueError(f"unknown recall mode: {mode!r} (lexical|semantic|hybrid|auto)")
