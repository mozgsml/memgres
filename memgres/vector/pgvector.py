"""pgvector backend: chunk vectors live in the ``memory_segment`` table.

There is no whole-body doc vector — each memory is stored as its chunks, and
recall ranks over them (best chunk per memory). Because the chunks reference the
memory row, ``forget`` (which DELETEs the row) drops them via ON DELETE CASCADE.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .base import Hit, _vec_literal, as_namespaces, grouped_chunk_search


class PgvectorBackend:
    # ─── chunk store ──────────────────────────────────────────────────────────
    def index_chunks(self, conn, memory_id: str, ns: str, src_hash: str,
                     chunks: Sequence[Tuple[int, int, int, Sequence[float]]]
                     ) -> None:
        # Replace-all: a re-embed after an edit leaves no stale seqs behind.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_segment WHERE memory_id=%s", (memory_id,))
            rows = [(memory_id, seq, s, e, _vec_literal(vec), src_hash, ns)
                    for (seq, s, e, vec) in chunks]
            if rows:
                cur.executemany(
                    "INSERT INTO memory_segment "
                    "(memory_id, seq, seg_start, seg_end, embedding, src_hash, namespace) "
                    "VALUES (%s,%s,%s,%s,%s::vector,%s,%s)",
                    rows)

    def delete_chunks(self, conn, memory_id: str, ns: str) -> None:
        # The FK cascade also clears these on `forget`; explicit for parity.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_segment WHERE memory_id=%s", (memory_id,))

    def chunk_src_hash(self, conn, memory_id: str, ns: str) -> Optional[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT src_hash FROM memory_segment "
                "WHERE memory_id=%s AND namespace=%s LIMIT 1",
                (memory_id, ns))
            row = cur.fetchone()
        return row[0] if row else None

    def retag_namespace(self, conn, old_ns: str, new_ns: str) -> int:
        # Same database, so this rides in the caller's transaction and commits
        # or rolls back with the memories themselves.
        with conn.cursor() as cur:
            cur.execute("UPDATE memory_segment SET namespace=%s WHERE namespace=%s",
                        (new_ns, old_ns))
            return cur.rowcount

    # ─── grouped semantic ranking ─────────────────────────────────────────────
    def search(self, conn, cfg, query_vec: Sequence[float], k: int, ns,
               tags: Optional[Sequence[str]], path_prefix: Optional[str]) -> List[Hit]:
        qv = _vec_literal(query_vec)

        def fetch_chunks(overfetch: int, exclude: List[str]):
            # `= ANY` over the same normalized set the Postgres row filter uses,
            # so ranking and filtering can't be scoped differently.
            where = "namespace = ANY(%s)"
            params: list = [as_namespaces(ns)]
            if exclude:
                where += " AND memory_id <> ALL(%s)"
                params.append(exclude)
            sql = (
                "SELECT memory_id, seg_start, seg_end, "
                "1 - (embedding <=> %s::vector) AS score "
                f"FROM memory_segment WHERE {where} "
                "ORDER BY embedding <=> %s::vector ASC LIMIT %s"
            )
            args = [qv] + params + [qv, overfetch]
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return [(str(r[0]), int(r[1]), int(r[2]), float(r[3]))
                        for r in cur.fetchall()]

        return grouped_chunk_search(conn, ns, k, tags, path_prefix, fetch_chunks)
