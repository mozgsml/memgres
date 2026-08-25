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


# How much harder a hit in the curated title counts than one in the body. A
# caption is written to say what the memory IS, so matching it is the stronger
# signal — but only a nudge, not a separate stage: a memory that matches in both
# should still outrank one that matches the title alone, and a two-pass
# "titles first, then bodies" ordering cannot express that.
TITLE_WEIGHT = 2.0


def _lexical(conn, cfg, ns, query, k, tags, path_prefix,
             match: Optional[str] = None, tags_match: str = "all") -> List[Hit]:
    """Rank by the query against BOTH the body and the curated title.

    Titles used to be searchable only through a separate `find` tool, which meant
    the two halves of "where is it" lived in different places and a caller had to
    guess which to reach for — and an untitled corpus made the title half answer
    "nothing found" to everything. One query over both, with the title weighted.

    Both ranks are COALESCEd because a tsvector column can be NULL — rows written
    before `title_fts` existed carry one, and `ts_rank(NULL, q)` is NULL, which
    would make the whole sum NULL. Such a row still MATCHES (`fts @@ q OR NULL`
    is true when the body matches), NULL sorts first under `ORDER BY score DESC`,
    and the score then reaches `float(None)` — a TypeError no transport catches,
    so one legacy row would 500 every lexical recall in the deployment. The
    backfill in `schema.py` removes those NULLs; this makes the arithmetic
    correct even if one ever appears again."""
    tsq, qtext = _tsquery(cfg, match, query)
    where, params = build_filters(ns, tags, path_prefix, tags_match)
    lang = cfg.fts_language
    sql = (
        f"SELECT {HIT_COLUMNS}, "
        f"COALESCE(ts_rank(title_fts, {tsq}), 0) * {TITLE_WEIGHT} "
        f"+ COALESCE(ts_rank(fts, {tsq}), 0) AS score "
        f"FROM memory WHERE {where} "
        f"AND (fts @@ {tsq} OR title_fts @@ {tsq}) "
        "ORDER BY score DESC LIMIT %s"
    )
    args = ([lang, qtext, lang, qtext] + params
            + [lang, qtext, lang, qtext, k])
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return [row_to_hit(r, r[-1]) for r in cur.fetchall()]   # score = trailing col


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
           full_body: Optional[bool] = None, bodies: bool = True,
           tags_match: str = "all") -> List[Hit]:
    if mode == "auto":
        mode = "semantic" if backend else "lexical"
    if mode == "lexical":
        hits = _lexical(conn, cfg, ns, query, k, tags, path_prefix, match,
                        tags_match)
    elif mode == "semantic":
        if backend is None:
            raise RuntimeError(
                "semantic recall needs an embedder (MEMGRES_EMBED_PROVIDER)")
        hits = backend.search(conn, cfg, embedder.embed_query(query), k, ns,
                              tags, path_prefix, tags_match)
    elif mode == "hybrid":
        if backend is None:
            raise RuntimeError(
                "semantic recall needs an embedder (MEMGRES_EMBED_PROVIDER)")
        lex = _lexical(conn, cfg, ns, query, k, tags, path_prefix, match,
                       tags_match)
        sem = backend.search(conn, cfg, embedder.embed_query(query), k, ns,
                             tags, path_prefix, tags_match)
        hits = _rrf([sem, lex], k)
    else:
        raise ValueError(
            f"unknown recall mode: {mode!r} (lexical|semantic|hybrid|auto)")
    if not bodies:
        # The light pass: rank, then answer with WHERE things are and nothing
        # more. No ts_headline round-trip, no chunk slicing, no body text on the
        # wire — cheap enough to scan a large result set before deciding what to
        # actually read. (The bodies were still SELECTed for ranking; dropping
        # them is about what leaves this function, not about the query.)
        for h in hits:
            h.body = None
            h.snippet = None
            h.kind = None
            h.lines = None
        return hits
    return attach_snippets(conn, cfg, ns, query, hits,
                           snippet=snippet, full_body=full_body)
