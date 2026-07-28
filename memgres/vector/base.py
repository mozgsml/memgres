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
    body: Optional[str]           # the whole body; None when full_body is off
    tags: List[str]
    path: Optional[str]
    score: float
    # filled in by search.attach_snippets after ranking: the most relevant slice
    # of the body plus its 1-based line (line is None for the ts_headline path,
    # which has no offset). Trailing defaults keep Hit(id, body, tags, path,
    # score) call sites in the backends working unchanged.
    snippet: Optional[str] = None
    line: Optional[int] = None


def _vec_literal(vec: Sequence[float]) -> str:
    """pgvector text literal for a float sequence: ``[a,b,c]``."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


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

    def get_segments(self, conn, memory_id: str, src_hash: str
                     ) -> Optional[List[Tuple[int, int, List[float]]]]:
        """Fresh cached segments as ``(seq, seg_start, seg_end, vector)`` sorted
        by seq, or ``None`` when absent OR stale (stored src_hash != requested),
        signalling the caller to recompute."""
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
