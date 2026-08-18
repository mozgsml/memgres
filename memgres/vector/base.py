"""The vector-backend interface plus the bits store/search/backends all share.

A ``VectorBackend`` hides *where* chunk vectors live (in the ``memory_segment``
table, or out-of-band in Qdrant) behind a few operations, so ``store.py`` and
``search.py`` never branch on the backend. ``make_backend`` picks the concrete
backend from config, or returns ``None`` when there are no vectors to manage (no
embedder).

**Chunks are the semantic index.** A memory is split into overlapping chunks
(``segments.segment``); each chunk is embedded and stored with its offset span,
the memory's ``src_hash`` (content_hash), and its namespace. Recall ranks over
the chunk vectors and keeps the single best chunk per memory (``search`` groups
internally), so a long body's tail is searchable and one memory yields one hit —
whose winning chunk is also its snippet. There is no whole-body doc vector.

This module must not import from ``store`` or ``search`` — they import from it —
so the shared helpers (``Hit``, ``build_filters``, the grouped-search driver)
live here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

_log = logging.getLogger("memgres.vector")

# Grouped chunk-search tuning. A memory can own many chunks, so a single ANN
# fetch of ``k`` chunks may map to fewer than ``k`` distinct memories (or, after
# tag/tree/expiry filtering in Postgres, fewer still). We over-fetch, group to
# distinct memories, and if short, fetch again EXCLUDING the memories already
# found — up to ``MAX_ROUNDS`` times — so one 500-chunk document can't crowd out
# everything else. The round cap bounds the work; a short result is logged, never
# silently truncated.
OVERFETCH_MULT = 10
OVERFETCH_CAP = 500
MAX_ROUNDS = 4


@dataclass
class Hit:
    id: str
    body: Optional[str]           # working copy for snippet extraction; always
                                  # dropped to None before output (see `snippet`)
    tags: List[str]
    path: Optional[str]
    score: float
    title: str = ""               # curated caption (from HIT_COLUMNS)
    # filled in by search.attach_snippets after ranking. `snippet` is the text we
    # return: the most relevant slice, or the whole body when it's short / forced.
    # `kind` says which — "full" (snippet IS the entire body) vs "snippet" (a
    # slice). `lines` is the 1-based inclusive line range the snippet spans, or
    # None for the ts_headline path (Postgres gives no offset). `body` is dropped
    # before output — the snippet field carries the returned text. Trailing
    # defaults keep row_to_hit the single construction point.
    snippet: Optional[str] = None
    kind: Optional[str] = None
    lines: Optional[List[int]] = None
    # set by a backend's grouped search: the (start, end) char offsets of the
    # winning chunk, so attach_snippets slices the snippet with no re-embedding.
    chunk_span: Optional[Tuple[int, int]] = None


def _vec_literal(vec: Sequence[float]) -> str:
    """pgvector text literal for a float sequence: ``[a,b,c]``."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# The memory columns every ranked hit needs, in row_to_hit read-order. One
# definition so lexical and both chunk backends SELECT the same set and adding a
# field is one edit. When a backend ranks in SQL and appends its score column
# AFTER these, the score is row[len(HIT_COLUMNS)].
HIT_COLUMNS = "id, body, tags, path::text, title"


def row_to_hit(row, score: float) -> "Hit":
    """Build a Hit from a (HIT_COLUMNS) row plus a separately-supplied score."""
    return Hit(str(row[0]), row[1], list(row[2]), row[3], float(score), title=row[4])


def build_filters(ns: str, tags: Optional[Sequence[str]], path_prefix: Optional[str]):
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


def fetch_hit_rows(conn, ns: str, memory_ids: Sequence[str],
                   tags: Optional[Sequence[str]],
                   path_prefix: Optional[str]) -> dict:
    """Bodies (HIT_COLUMNS) for the given memory ids, applying the shared
    tag/tree/expiry/namespace filters in Postgres. Returns ``{id: row}`` for
    exactly the rows that pass — a candidate whose tags/subtree/expiry excludes it
    simply won't appear. Both chunk backends fetch bodies from Postgres, so this
    lives here once."""
    if not memory_ids:
        return {}
    where, params = build_filters(ns, tags, path_prefix)
    sql = f"SELECT {HIT_COLUMNS} FROM memory WHERE {where} AND id = ANY(%s)"
    with conn.cursor() as cur:
        cur.execute(sql, params + [list(memory_ids)])
        return {str(r[0]): r for r in cur.fetchall()}


def grouped_chunk_search(conn, ns: str, k: int,
                         tags: Optional[Sequence[str]],
                         path_prefix: Optional[str],
                         fetch_chunks: Callable[[int, List[str]], List[tuple]]
                         ) -> List[Hit]:
    """Rank over chunk vectors and return at most ``k`` hits, one per memory (its
    best chunk), tag/tree/expiry filtered.

    ``fetch_chunks(overfetch, exclude_ids)`` is the backend's only job: return up
    to ``overfetch`` top chunks as ``(memory_id, start, end, score)`` in
    descending score, skipping any memory in ``exclude_ids``. This driver handles
    grouping to distinct memories, the Postgres filter, and the iterative-exclude
    loop that keeps one huge document from starving the rest. First-seen wins per
    memory: since chunks arrive in descending score, the first chunk seen for a
    memory is its best, and a memory is excluded once grouped, so scores are never
    revised — a final sort by score gives the correct global order."""
    overfetch = min(max(k * OVERFETCH_MULT, k), OVERFETCH_CAP)
    survivors: dict = {}      # memory_id -> Hit (passed the PG filter)
    seen: set = set()         # every memory grouped so far (passed or filtered)
    exhausted = False
    for _round in range(MAX_ROUNDS):
        chunks = fetch_chunks(overfetch, list(seen))
        if not chunks:
            exhausted = True
            break
        best: dict = {}       # this round's new memories -> (score, start, end)
        for memid, s, e, sc in chunks:
            if memid in seen or memid in best:
                continue
            best[memid] = (sc, s, e)
        if not best:
            exhausted = True     # everything returned was already excluded
            break
        rows = fetch_hit_rows(conn, ns, list(best.keys()), tags, path_prefix)
        for memid, (sc, s, e) in best.items():
            seen.add(memid)
            row = rows.get(memid)
            if row is not None:
                h = row_to_hit(row, sc)
                h.chunk_span = (s, e)
                survivors[memid] = h
        if len(survivors) >= k:
            break
    hits = sorted(survivors.values(), key=lambda h: h.score, reverse=True)
    if len(hits) < k and not exhausted:
        _log.info("grouped chunk search hit the round cap (%d) with %d/%d distinct "
                  "memories after filtering; returning those", MAX_ROUNDS, len(hits), k)
    return hits[:k]


class VectorBackend(Protocol):
    """Chunk-vector storage + grouped semantic ranking. One memory ⇒ many chunks;
    ranking keeps the best chunk per memory."""

    def index_chunks(self, conn, memory_id: str, ns: str, src_hash: str,
                     chunks: Sequence[Tuple[int, int, int, Sequence[float]]]
                     ) -> None:
        """Replace every chunk vector for ``memory_id`` with ``chunks`` — a list
        of ``(seq, start, end, vector)`` stamped with ``src_hash`` (the memory's
        content_hash) and ``ns``. Replace-all, so an edit leaves no stale chunk."""
        ...

    def delete_chunks(self, conn, memory_id: str, ns: str) -> None:
        """Drop every chunk vector for ``memory_id``."""
        ...

    def chunk_src_hash(self, conn, memory_id: str, ns: str) -> Optional[str]:
        """The ``src_hash`` the stored chunks were built from, or ``None`` when a
        memory has no chunks. Lets the embed worker skip a row whose chunks are
        already current (idempotent drain)."""
        ...

    def search(self, conn, cfg, query_vec: Sequence[float], k: int, ns: str,
               tags: Optional[Sequence[str]], path_prefix: Optional[str]
               ) -> List[Hit]:
        """Top-``k`` memories by best-chunk similarity, tag/tree/expiry filtered,
        each hit carrying its winning chunk's ``chunk_span``."""
        ...


def make_backend(cfg, embedder):
    """Return a VectorBackend, or None when there are no vectors to manage
    (embedder is None). Picks qdrant when cfg.vector_backend=='qdrant', else pgvector."""
    if embedder is None:
        return None
    if cfg.vector_backend == "qdrant":
        from .qdrant import QdrantBackend
        return QdrantBackend(embedder.dim)
    from .pgvector import PgvectorBackend
    return PgvectorBackend()
