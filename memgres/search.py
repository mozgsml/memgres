"""Recall: lexical (Postgres FTS), semantic (vector backend), or hybrid (RRF).

All three share the same filters — namespace, tags (``@>`` contains-all), tree
subtree (``path <@ prefix``), and not-expired — so you can scope any query to a
branch of the tree or a set of tags. ``mode='auto'`` picks semantic when a
vector backend is present (an embedder is configured), else lexical.

Semantic ranking lives behind a :class:`~memgres.vector.VectorBackend` (pgvector
in-row, or Qdrant out-of-band); this module never branches on which one. Hybrid
fuses the lexical and semantic ranked lists with Reciprocal Rank Fusion (RRF):
exact identifiers that dense vectors fumble come in via lexical, meaning-based
matches via vectors, and neither backend needs to know about the other.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from .vector.base import Hit, build_filters

RRF_K = 60  # standard RRF damping constant


def _lexical(conn, cfg, ns, query, k, tags, path_prefix) -> List[Hit]:
    where, params = build_filters(ns, tags, path_prefix)
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
           mode: str = "auto", backend=None) -> List[Hit]:
    if mode == "auto":
        mode = "semantic" if backend else "lexical"
    if mode == "lexical":
        return _lexical(conn, cfg, ns, query, k, tags, path_prefix)
    if mode == "semantic":
        if backend is None:
            raise RuntimeError(
                "semantic recall needs an embedder (MEMGRES_EMBED_PROVIDER)")
        return backend.search(conn, cfg, embedder.embed_query(query), k, ns,
                              tags, path_prefix)
    if mode == "hybrid":
        if backend is None:
            raise RuntimeError(
                "semantic recall needs an embedder (MEMGRES_EMBED_PROVIDER)")
        lex = _lexical(conn, cfg, ns, query, k, tags, path_prefix)
        sem = backend.search(conn, cfg, embedder.embed_query(query), k, ns,
                             tags, path_prefix)
        return _rrf([sem, lex], k)
    raise ValueError(f"unknown recall mode: {mode!r} (lexical|semantic|hybrid|auto)")
