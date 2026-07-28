"""pgvector backend: vectors live in the ``memory.embedding`` column.

Because the vector is in the same row as the body, tag/tree/TTL filters and the
ANN ranking are one filtered query, and ``forget`` (which DELETEs the row) drops
the vector for free — so ``delete_doc`` is a no-op here.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from .base import Hit, _vec_literal, build_filters


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
