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

from .diffing import content_hash
from .segments import segment
from .vector.base import HIT_COLUMNS, Hit, build_filters, row_to_hit

RRF_K = 60  # standard RRF damping constant


def _lexical(conn, cfg, ns, query, k, tags, path_prefix,
             match: Optional[str] = None) -> List[Hit]:
    match = match or cfg.lexical_match
    # One tsquery expression, reused in both the ts_rank score and the @@ filter.
    #   all -> plainto_tsquery: every word ANDed (narrow; current behavior).
    #   any -> websearch_to_tsquery over the words joined by " or ": OR-any
    #          (forgiving). websearch_to_tsquery is injection-safe and reads
    #          bare `or` as the OR operator, so user text can't inject syntax.
    if match == "all":
        tsq = "plainto_tsquery(%s::regconfig, %s)"
        qtext = query
    else:
        tsq = "websearch_to_tsquery(%s::regconfig, %s)"
        qtext = " or ".join(query.split())  # empty query -> "" -> no matches
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


def find(conn, cfg, ns: str, query: str, *, tags: Optional[Sequence[str]] = None,
         path_prefix: Optional[str] = None, k: int = 10,
         match: Optional[str] = None) -> List[dict]:
    """Locate memories whose TITLE matches — a light "where is it" search over the
    curated title only (``title_fts``), never the body. Same tag/subtree/namespace
    filters as recall (so it's multi-tenant safe by construction), but returns
    light rows ``{id, path, title, tags, score}`` — no body, no snippet, no vectors.
    Fast to scan before a heavier body recall, and works without an embedder."""
    match = match or cfg.lexical_match
    if match == "all":
        tsq = "plainto_tsquery(%s::regconfig, %s)"
        qtext = query
    else:
        tsq = "websearch_to_tsquery(%s::regconfig, %s)"
        qtext = " or ".join(query.split())
    where, params = build_filters(ns, tags, path_prefix)
    sql = (
        f"SELECT id, path::text, title, tags, ts_rank(title_fts, {tsq}) AS score "
        f"FROM memory WHERE {where} AND title_fts @@ {tsq} "
        "ORDER BY score DESC LIMIT %s"
    )
    args = [cfg.fts_language, qtext] + params + [cfg.fts_language, qtext, k]
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [{"id": str(r[0]), "path": r[1], "title": r[2],
                 "tags": list(r[3]), "score": float(r[4])} for r in cur.fetchall()]


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


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. Scale-invariant, so a backend that returns normalized
    vectors (qdrant does, under cosine distance) gives the same ranking as one
    that returns raw vectors (pgvector)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _ts_headline(conn, cfg, ns: str, query: str, hits: List[Hit]) -> dict:
    """One batched ts_headline over every hit id — the lexical/fallback snippet.
    Returns {id: headline}. ids come from the already-ns-scoped ranked hits, so
    the WHERE stays inside the caller's namespace (no cross-tenant read)."""
    ids = [h.id for h in hits]
    sql = (
        "SELECT id, ts_headline(%s::regconfig, body, "
        "plainto_tsquery(%s::regconfig, %s), "
        "'MaxWords=40, MinWords=15, ShortWord=3') "
        "FROM memory WHERE namespace=%s AND id = ANY(%s)"
    )
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql, [cfg.fts_language, cfg.fts_language, query, ns, ids])
        for r in cur.fetchall():
            out[str(r[0])] = r[1]
    return out


def _best_segment_snippet(conn, cfg, embedder, backend, ns: str, hit: Hit,
                          qvec: Sequence[float]) -> None:
    """Set ``hit.snippet``/``hit.line`` from the body segment most similar to the
    query. Uses the durable segment cache: compute+store on first sight of this
    body, reuse after, recompute when the body changed (src_hash mismatch)."""
    body = hit.body or ""
    if not body:
        hit.snippet, hit.line = "", 1
        return
    src = content_hash(body)
    segs = backend.get_segments(conn, hit.id, ns, src)  # (seq, start, end, vec) or None
    if segs is None:
        spans = segment(body, cfg.snippet_seg_chars, cfg.snippet_seg_overlap)
        vecs = embedder.embed_documents([body[s:e] for (s, e) in spans])
        segs = [(i, s, e, v)
                for i, ((s, e), v) in enumerate(zip(spans, vecs))]
        backend.upsert_segments(conn, hit.id, ns, src, segs)
    best_span = None
    best_score = None
    for (_seq, s, e, vec) in segs:
        sc = _cosine(qvec, vec)
        if best_score is None or sc > best_score:
            best_score, best_span = sc, (s, e)
    if best_span is None:
        hit.snippet, hit.line = body[:cfg.snippet_seg_chars], 1
        return
    bs, be = best_span
    hit.snippet = body[bs:be]
    hit.line = body[:bs].count("\n") + 1


def attach_snippets(conn, cfg, embedder, backend, ns: str, query: str, mode: str,
                    hits: List[Hit], *, snippet: Optional[bool],
                    full_body: Optional[bool]) -> List[Hit]:
    """After ranking, hang a snippet (+line) on each hit and optionally drop the
    full body. Semantic/hybrid hits pick their best-matching segment (embedded &
    cached per body-hash); everything else uses Postgres ``ts_headline``."""
    snippet = cfg.snippet if snippet is None else snippet
    full_body = cfg.full_body if full_body is None else full_body

    if snippet and hits:
        use_semantic = (mode in ("semantic", "hybrid") and cfg.snippet_semantic
                        and backend is not None and embedder is not None)
        if use_semantic:
            qvec = embedder.embed_query(query)   # once per call, not per hit
            for h in hits:
                _best_segment_snippet(conn, cfg, embedder, backend, ns, h, qvec)
        else:
            heads = _ts_headline(conn, cfg, ns, query, hits)  # one batched query
            for h in hits:
                snip = heads.get(h.id)
                if not snip:  # no match highlighted → a short head of the body
                    snip = (h.body or "")[:200]
                h.snippet = snip
                h.line = None  # ts_headline gives no offset — fine

    if not full_body:  # after snippets: the best-segment path needs the body
        for h in hits:
            h.body = None
    return hits


def recall(conn, cfg, embedder, ns: str, query: str, *, k: int = 10,
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
    return attach_snippets(conn, cfg, embedder, backend, ns, query, mode, hits,
                           snippet=snippet, full_body=full_body)
