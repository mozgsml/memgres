"""The vector-backend interface plus the bits store/search/backends all share.

A ``VectorBackend`` hides *where* vectors live (in the ``memory.embedding``
column, or out-of-band in Qdrant) behind three doc-level operations, so
``store.py`` and ``search.py`` never branch on the backend. ``make_backend``
picks the concrete backend from config, or returns ``None`` when there are no
vectors to manage (no embedder).

This module must not import from ``store`` or ``search`` — they import from it —
so the shared helpers (``Hit``, ``build_filters``, ``_vec_literal``) live here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence, Tuple


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


def _vec_literal(vec: Sequence[float]) -> str:
    """pgvector text literal for a float sequence: ``[a,b,c]``."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# The memory columns every ranked hit needs, in row_to_hit read-order. One
# definition so lexical/pgvector/qdrant all SELECT the same set and adding a field
# is one edit. Backends that also rank in SQL append their score column AFTER
# these, so the score is row[len(HIT_COLUMNS)].
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


class VectorBackend(Protocol):
    def upsert_doc(self, conn, id: str, vec: Sequence[float], ns: str) -> None: ...
    def delete_doc(self, conn, id: str, ns: str) -> None: ...
    def search(self, conn, cfg, query_vec: Sequence[float], k: int, ns: str,
               tags: Optional[Sequence[str]], path_prefix: Optional[str]) -> List[Hit]: ...

    # ─── segment vectors (durable per-memory cache for semantic snippets) ─────
    def upsert_segments(self, conn, memory_id: str, ns: str, src_hash: str,
                        segments: Sequence[Tuple[int, int, int, Sequence[float]]]
                        ) -> None:
        """Replace all cached segments for ``memory_id``. ``segments`` is a list
        of ``(seq, seg_start, seg_end, vector)``, all stamped with ``src_hash``
        (the memory's content_hash) + ``ns``."""
        ...

    def get_segments(self, conn, memory_id: str, ns: str, src_hash: str
                     ) -> Optional[List[Tuple[int, int, List[float]]]]:
        """Fresh cached segments as ``(seq, seg_start, seg_end, vector)`` sorted
        by seq, or ``None`` when absent OR stale (stored src_hash != requested),
        signalling the caller to recompute. Scoped by ``ns`` as well as
        ``memory_id``: defense-in-depth so a tenant never reads another's
        segment cache even if a foreign memory_id were somehow supplied."""
        ...

    def delete_segments(self, conn, memory_id: str) -> None:
        """Drop every cached segment for ``memory_id``."""
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
