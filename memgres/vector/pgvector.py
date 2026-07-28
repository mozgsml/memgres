"""pgvector backend: vectors live in the ``memory.embedding`` column.

Because the vector is in the same row as the body, tag/tree/TTL filters and the
ANN ranking are one filtered query, and ``forget`` (which DELETEs the row) drops
the vector for free — so ``delete_doc`` is a no-op here.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .base import Hit, _vec_literal, build_filters


def _parse_vec(text: str) -> List[float]:
    """Parse a pgvector text literal (``[a,b,c]``) back to a list of floats."""
    s = text.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    s = s.strip()
    return [float(x) for x in s.split(",")] if s else []


class PgvectorBackend:
    def upsert_doc(self, conn, id: str, vec: Sequence[float], ns: str) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memory SET embedding=%s::vector WHERE id=%s AND namespace=%s",
                (_vec_literal(vec), id, ns))

    def delete_doc(self, conn, id: str, ns: str) -> None:
        # No-op: the vector is the memory row's `embedding` column, which
        # `forget` already DELETEs. Nothing out-of-band to clean up.
        pass

    def search(self, conn, cfg, query_vec: Sequence[float], k: int, ns: str,
               tags: Optional[Sequence[str]], path_prefix: Optional[str]) -> List[Hit]:
        qv = _vec_literal(query_vec)
        where, params = build_filters(ns, tags, path_prefix)
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

    # ─── segment vectors (memory_segment table) ───────────────────────────────
    def upsert_segments(self, conn, memory_id: str, ns: str, src_hash: str,
                        segments: Sequence[Tuple[int, int, int, Sequence[float]]]
                        ) -> None:
        # Replace-all: a re-embed after an edit leaves no stale seqs behind.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_segment WHERE memory_id=%s", (memory_id,))
            rows = [(memory_id, seq, s, e, _vec_literal(vec), src_hash, ns)
                    for (seq, s, e, vec) in segments]
            if rows:
                cur.executemany(
                    "INSERT INTO memory_segment "
                    "(memory_id, seq, seg_start, seg_end, embedding, src_hash, namespace) "
                    "VALUES (%s,%s,%s,%s,%s::vector,%s,%s)",
                    rows)

    def get_segments(self, conn, memory_id: str, src_hash: str
                     ) -> Optional[List[Tuple[int, int, int, List[float]]]]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seq, seg_start, seg_end, embedding::text FROM memory_segment "
                "WHERE memory_id=%s AND src_hash=%s ORDER BY seq",
                (memory_id, src_hash))
            rows = cur.fetchall()
        if not rows:
            return None  # absent OR src_hash mismatch → caller recomputes
        return [(r[0], r[1], r[2], _parse_vec(r[3])) for r in rows]

    def delete_segments(self, conn, memory_id: str) -> None:
        # The FK cascade also clears these on `forget`; explicit for parity.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_segment WHERE memory_id=%s", (memory_id,))
