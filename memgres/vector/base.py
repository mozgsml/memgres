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
    namespace: str = ""           # which namespace this hit came from. Carried on
                                  # every hit because a recall may span several —
                                  # without it the caller cannot tell where a
                                  # result lives, nor address it for a follow-up.
    space: Optional[str] = None   # that namespace's NAME, filled in by the store
                                  # (the memory row holds only the id)
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

    def to_recall_dict(self) -> dict:
        """The recall wire shape — one definition, so the HTTP and MCP layers
        return the same keys and adding a field is a single edit. Omits the raw
        body (dropped by attach_snippets) and the internal chunk_span."""
        return {"id": self.id, "title": self.title, "tags": self.tags,
                "path": self.path, "score": self.score, "snippet": self.snippet,
                "kind": self.kind, "lines": self.lines,
                "space_id": self.namespace, "space": self.space}


def _vec_literal(vec: Sequence[float]) -> str:
    """pgvector text literal for a float sequence: ``[a,b,c]``."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# The memory columns every ranked hit needs, in row_to_hit read-order. One
# definition so lexical and both chunk backends SELECT the same set and adding a
# field is one edit. When a backend ranks in SQL and appends its score column
# AFTER these, the score is row[len(HIT_COLUMNS)].
HIT_COLUMNS = "id, body, tags, path::text, title, namespace"


def row_to_hit(row, score: float) -> "Hit":
    """Build a Hit from a (HIT_COLUMNS) row plus a separately-supplied score."""
    return Hit(str(row[0]), row[1], list(row[2]), row[3], float(score),
               title=row[4], namespace=str(row[5]))


def as_namespaces(ns) -> List[str]:
    """Normalize a namespace address to a list of ids.

    A read may span several namespaces (see ``identity.resolve_spaces``), so every
    filter below takes a *set*. A bare ``str`` is ONE namespace — never a sequence
    of characters, which is the bug this function exists to make impossible."""
    if isinstance(ns, str):
        return [ns]
    return [str(n) for n in ns]


def build_filters(ns, tags: Optional[Sequence[str]], path_prefix: Optional[str],
                  tags_match: str = "all"):
    """Return (sql_fragment, params) for the shared WHERE tail.

    ``ns`` is one namespace id or several. The tenant predicate is ``= ANY`` in
    both cases: one code path, so a multi-namespace read cannot diverge from a
    single-namespace one. ``fetch_hit_rows`` re-applies it to every candidate a
    vector backend proposes, which is what keeps a backend-side filter bug a
    recall problem rather than a cross-tenant leak.

    This is where the predicate belongs for anything that reads memory ROWS. Two
    reads cannot use it and write their own instead, and each says why on the
    spot: ``search._ts_headline`` (it re-checks ids that are already scoped) and
    ``tags.tag_counts`` (it aggregates over ``unnest(tags)``, not over rows).
    Both are covered by tenancy tests; a third exception should not appear
    without one."""
    sql = ["namespace = ANY(%s)", "(expires_at IS NULL OR expires_at > now())"]
    params: list = [as_namespaces(ns)]
    from ..tags import check_tag_match, normalize_tags
    # Normalised on BOTH sides — a filter written `X402` has to find a row stored
    # as `x402`, and neither side can be trusted to have done it.
    wanted = normalize_tags(tags)
    mode = check_tag_match(tags_match)
    if wanted:
        op = "@>" if mode == "all" else "&&"
        sql.append(f"tags {op} %s")        # @> every requested tag · && any of them
        params.append(wanted)
    # A request that normalises to nothing (`[]`, or only blanks) adds NO
    # predicate. Left to the operators it would mean opposite things: `tags @>
    # '{}'` is true of every row and `tags && '{}'` is true of none, so the same
    # input would filter nothing or everything depending on a mode flag the
    # caller may not have set. Neither is an answer; "no tags requested" is.
    if path_prefix:
        # Checked here, not by Postgres: an unvalidated prefix came back as
        # `ltree syntax error at character 1`, which tells the caller nothing
        # about what they passed.
        from ..paths import check_path
        sql.append("path <@ %s::ltree")     # subtree of the prefix
        params.append(check_path(path_prefix, "path_prefix"))
    return " AND ".join(sql), params


def fetch_hit_rows(conn, ns, memory_ids: Sequence[str],
                   tags: Optional[Sequence[str]],
                   path_prefix: Optional[str],
                   tags_match: str = "all") -> dict:
    """Bodies (HIT_COLUMNS) for the given memory ids, applying the shared
    tag/tree/expiry/namespace filters in Postgres. Returns ``{id: row}`` for
    exactly the rows that pass — a candidate whose tags/subtree/expiry excludes it
    simply won't appear. Both chunk backends fetch bodies from Postgres, so this
    lives here once."""
    if not memory_ids:
        return {}
    where, params = build_filters(ns, tags, path_prefix, tags_match)
    sql = f"SELECT {HIT_COLUMNS} FROM memory WHERE {where} AND id = ANY(%s)"
    with conn.cursor() as cur:
        cur.execute(sql, params + [list(memory_ids)])
        return {str(r[0]): r for r in cur.fetchall()}


def grouped_chunk_search(conn, ns, k: int,
                         tags: Optional[Sequence[str]],
                         path_prefix: Optional[str],
                         fetch_chunks: Callable[[int, List[str]], List[tuple]],
                         tags_match: str = "all") -> List[Hit]:
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
        rows = fetch_hit_rows(conn, ns, list(best.keys()), tags, path_prefix,
                              tags_match)
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

    def retag_namespace(self, conn, old_ns: str, new_ns: str) -> int:
        """Move every chunk vector from one namespace to another; returns how
        many moved. Used when adopting orphaned single-mode data: the chunk
        index carries its own copy of the namespace, and leaving it behind makes
        semantic recall return NOTHING for the adopted rows — silently, since a
        filter that matches no points is not an error."""
        ...

    def search(self, conn, cfg, query_vec: Sequence[float], k: int, ns: str,
               tags: Optional[Sequence[str]], path_prefix: Optional[str],
               tags_match: str = "all") -> List[Hit]:
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
