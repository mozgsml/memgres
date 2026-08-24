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

from .vector.base import (HIT_COLUMNS, Hit, as_namespaces, build_filters,
                          row_to_hit)

RRF_K = 60  # standard RRF damping constant


def _tsquery(cfg, match: Optional[str], query: str):
    """The tsquery SQL fragment + the query text to bind, shared by every lexical
    search (body recall and title find). One expression is reused in both the
    ``ts_rank`` score and the ``@@`` filter of a query.

      all -> plainto_tsquery: every word ANDed (narrow).
      any -> websearch_to_tsquery over the words joined by " or ": OR-any
             (forgiving; default). websearch_to_tsquery is injection-safe and
             reads a bare ``or`` as the operator, so user text can't inject syntax.

    An empty query yields ``""`` → matches nothing (never everything)."""
    match = match or cfg.lexical_match
    if match == "all":
        return "plainto_tsquery(%s::regconfig, %s)", query
    return "websearch_to_tsquery(%s::regconfig, %s)", " or ".join(query.split())


def _lexical(conn, cfg, ns, query, k, tags, path_prefix,
             match: Optional[str] = None) -> List[Hit]:
    tsq, qtext = _tsquery(cfg, match, query)
    where, params = build_filters(ns, tags, path_prefix)
    sql = (
        f"SELECT {HIT_COLUMNS}, "
        f"ts_rank(fts, {tsq}) AS score "
        f"FROM memory WHERE {where} "
        f"AND fts @@ {tsq} "
        "ORDER BY score DESC LIMIT %s"
    )
    args = [cfg.fts_language, qtext] + params + [cfg.fts_language, qtext, k]
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [row_to_hit(r, r[-1]) for r in cur.fetchall()]   # score = trailing col


def find(conn, cfg, ns, query: str, *, tags: Optional[Sequence[str]] = None,
         path_prefix: Optional[str] = None, k: int = 10,
         match: Optional[str] = None) -> List[dict]:
    """Locate memories whose TITLE matches — a light "where is it" search over the
    curated title only (``title_fts``), never the body. Same tag/subtree/namespace
    filters as recall (so it's multi-tenant safe by construction), but returns
    light rows ``{id, path, title, tags, score}`` — no body, no snippet, no vectors.
    Fast to scan before a heavier body recall, and works without an embedder."""
    tsq, qtext = _tsquery(cfg, match, query)
    where, params = build_filters(ns, tags, path_prefix)
    sql = (
        f"SELECT id, path::text, title, tags, namespace, "
        f"ts_rank(title_fts, {tsq}) AS score "
        f"FROM memory WHERE {where} AND title_fts @@ {tsq} "
        "ORDER BY score DESC LIMIT %s"
    )
    args = [cfg.fts_language, qtext] + params + [cfg.fts_language, qtext, k]
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [{"id": str(r[0]), "path": r[1], "title": r[2],
                 "tags": list(r[3]), "space_id": str(r[4]), "score": float(r[5])}
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


def _ts_headline(conn, cfg, ns, query: str, hits: List[Hit]) -> dict:
    """One batched ts_headline over every hit id — the lexical/fallback snippet.
    Returns {id: headline}. ids come from the already-ns-scoped ranked hits, so
    the WHERE stays inside the caller's namespaces (no cross-tenant read) — it is
    re-applied here rather than trusted, because this query reads BODIES and is
    the one read path that doesn't go through ``build_filters``.
    ``StartSel``/``StopSel`` are emptied so the snippet is clean prose — no
    ``<b>`` markers to mislead the model reading it."""
    ids = [h.id for h in hits]
    sql = (
        "SELECT id, ts_headline(%s::regconfig, body, "
        "plainto_tsquery(%s::regconfig, %s), "
        "'MaxWords=40, MinWords=15, ShortWord=3, StartSel=\"\", StopSel=\"\"') "
        "FROM memory WHERE namespace = ANY(%s) AND id = ANY(%s)"
    )
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql, [cfg.fts_language, cfg.fts_language, query,
                          as_namespaces(ns), ids])
        for r in cur.fetchall():
            out[str(r[0])] = r[1]
    return out


def _lines_for(body: str, start: int, end: int) -> List[int]:
    """1-based inclusive line range that the ``body[start:end]`` slice spans."""
    if end <= start:
        n = body.count("\n", 0, start) + 1
        return [n, n]
    return [body.count("\n", 0, start) + 1,
            body.count("\n", 0, end - 1) + 1]


def _set_full(hit: Hit, body: str) -> None:
    """Return the whole body as the snippet (it's short, or the caller forced it)."""
    hit.snippet = body
    hit.kind = "full"
    hit.lines = [1, body.count("\n") + 1] if body else [1, 1]


def _set_chunk(hit: Hit, body: str) -> None:
    """Slice the snippet from the winning chunk's span (already chosen by the
    grouped chunk ranking — no re-embedding on the read path)."""
    bs, be = hit.chunk_span
    bs = max(0, min(bs, len(body)))
    be = max(bs, min(be, len(body)))
    hit.snippet = body[bs:be]
    hit.lines = _lines_for(body, bs, be)
    hit.kind = "snippet"


def attach_snippets(conn, cfg, ns, query: str, hits: List[Hit], *,
                    snippet: Optional[bool], full_body: Optional[bool]) -> List[Hit]:
    """After ranking, give every hit exactly one body view — never both a slice
    and the whole thing. A hit gets the WHOLE body (``kind="full"``) when the
    caller opts out of snippeting (``snippet=False``), forces ``full_body=True``,
    or the body is short enough (≤ ``full_body_max_chars``) that a slice would
    just repeat it. Otherwise it gets a SLICE (``kind="snippet"``): a semantic hit
    slices its winning chunk (chosen during ranking, so no re-embedding here); a
    lexical hit uses Postgres ``ts_headline``. The raw ``body`` field is always
    dropped — ``snippet`` carries the returned text, ``kind`` says which view."""
    snippet = cfg.snippet if snippet is None else snippet
    full_body = cfg.full_body if full_body is None else full_body

    # Whole-body hits and semantic-chunk hits are settled inline; only the ones
    # needing a Postgres ts_headline slice are collected for the batched query.
    lexical_hits: List[Hit] = []
    for h in hits:
        body = h.body or ""
        if full_body or not snippet or len(body) <= cfg.full_body_max_chars:
            _set_full(h, body)
        elif h.chunk_span is not None and cfg.snippet_semantic:
            _set_chunk(h, body)
        else:
            lexical_hits.append(h)

    if lexical_hits:
        heads = _ts_headline(conn, cfg, ns, query, lexical_hits)  # one batched query
        for h in lexical_hits:
            snip = heads.get(h.id)
            if snip:
                h.snippet = snip
                h.lines = None           # ts_headline gives no offset
                h.kind = "snippet"
            else:                        # no highlighted match → bounded head slice
                body = h.body or ""
                h.snippet = body[:cfg.full_body_max_chars]
                h.lines = [1, h.snippet.count("\n") + 1]
                h.kind = "snippet"

    for h in hits:      # never leak the raw body as a separate field
        h.body = None
    return hits


def recall(conn, cfg, embedder, ns, query: str, *, k: int = 10,
           tags: Optional[Sequence[str]] = None, path_prefix: Optional[str] = None,
           mode: str = "auto", match: Optional[str] = None,
           backend=None, snippet: Optional[bool] = None,
           full_body: Optional[bool] = None) -> List[Hit]:
    if mode == "auto":
        mode = "semantic" if backend else "lexical"
    if mode == "lexical":
        hits = _lexical(conn, cfg, ns, query, k, tags, path_prefix, match)
    elif mode == "semantic":
        if backend is None:
            raise RuntimeError(
                "semantic recall needs an embedder (MEMGRES_EMBED_PROVIDER)")
        hits = backend.search(conn, cfg, embedder.embed_query(query), k, ns,
                              tags, path_prefix)
    elif mode == "hybrid":
        if backend is None:
            raise RuntimeError(
                "semantic recall needs an embedder (MEMGRES_EMBED_PROVIDER)")
        lex = _lexical(conn, cfg, ns, query, k, tags, path_prefix, match)
        sem = backend.search(conn, cfg, embedder.embed_query(query), k, ns,
                             tags, path_prefix)
        hits = _rrf([sem, lex], k)
    else:
        raise ValueError(
            f"unknown recall mode: {mode!r} (lexical|semantic|hybrid|auto)")
    return attach_snippets(conn, cfg, ns, query, hits,
                           snippet=snippet, full_body=full_body)
