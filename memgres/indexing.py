"""Chunk embedding: turn a memory body into chunk vectors, sync or via worker.

One place builds the chunk index for a memory (`index_memory`), reached two ways:

  * **synchronously**, inline in the write path (embedded/library mode, or a
    server with the worker turned off) — the default, so semantic recall never
    silently lags behind a write;
  * **asynchronously**, by the background embed worker draining `embed_pending`
    rows (the server's default: writes return fast, embedding runs off the
    request path).

Idempotent and crash-safe: `index_memory` skips a row whose chunks already match
the body's content_hash, and clears `embed_pending` only when the body it
embedded is still current — a concurrent edit bumps the hash and re-flags the
row, so it's picked up again rather than lost. `drain` commits per row, so a
crash mid-batch leaves the unfinished rows flagged for the next pass.
"""

from __future__ import annotations

import logging
from typing import Optional

from .diffing import content_hash
from .segments import segment

_log = logging.getLogger("memgres.indexing")


def index_memory(conn, cfg, embedder, backend, memory_id: str, body: str,
                 ns: str, src_hash: Optional[str] = None) -> bool:
    """Build and store the chunk vectors for one memory, then clear its pending
    flag. Returns True if it (re)embedded, False if the chunks were already
    current (or there's nothing to embed). Does NOT manage the transaction — the
    caller owns commit/rollback (the write path folds this into its own tx; the
    worker commits per row)."""
    src = src_hash or content_hash(body)
    if backend is None or embedder is None:
        _clear_pending(conn, memory_id, src)   # no vectors; don't leave it pending
        return False
    if backend.chunk_src_hash(conn, memory_id, ns) == src:
        _clear_pending(conn, memory_id, src)   # already current for this body
        return False
    spans = segment(body, cfg.chunk_chars, cfg.chunk_overlap)
    if spans:
        vecs = embedder.embed_documents([body[s:e] for (s, e) in spans])
        chunks = [(i, s, e, v) for i, ((s, e), v) in enumerate(zip(spans, vecs))]
        backend.index_chunks(conn, memory_id, ns, src, chunks)
    else:
        backend.delete_chunks(conn, memory_id, ns)   # empty body → no chunks
    _clear_pending(conn, memory_id, src)
    return True


def _clear_pending(conn, memory_id: str, src: str) -> None:
    """Clear ``embed_pending`` only if the body is still the one we embedded
    (guarded by content_hash). A concurrent edit changed the hash and re-set the
    flag, so leaving it set hands the row to the next pass."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE memory SET embed_pending=false "
            "WHERE id=%s AND content_hash=%s AND embed_pending",
            (memory_id, src))


def drain(conn, cfg, embedder, backend, limit: Optional[int] = None) -> int:
    """Embed pending memories oldest-first until none remain. Returns the count
    processed.

    Claim-based, so MANY workers (separate memgres-worker containers, or several
    server replicas each with an in-process worker) drain the same queue without
    duplicating work: each row is claimed with ``FOR UPDATE SKIP LOCKED`` inside
    the transaction that embeds it, so a concurrent worker skips a row already
    being handled rather than embedding it again or blocking on it. The row lock
    is held only for that one row's embed (sub-second) and released on commit;
    the flag is cleared in the same transaction, so a crash mid-embed rolls back
    and leaves the row pending for another pass (crash-safe). Also callable
    synchronously from a test/CLI (one worker, one connection).

    ``limit`` caps how many rows this call processes (default: drain everything);
    the worker loop calls it uncapped and relies on SKIP LOCKED for coordination."""
    cap = limit if limit is not None else None
    total = 0
    while cap is None or total < cap:
        # One row at a time: claim + lock (SKIP LOCKED) + embed + clear, all in
        # one transaction. Keeping it to a single row bounds how long any lock is
        # held and lets N workers fan out across the pending set.
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, body, namespace, content_hash FROM memory "
                        "WHERE embed_pending ORDER BY updated_at "
                        "LIMIT 1 FOR UPDATE SKIP LOCKED")
                    row = cur.fetchone()
                if row is None:
                    break                # nothing pending & unclaimed → done
                mid, body, ns, chash = row
                index_memory(conn, cfg, embedder, backend, str(mid),
                             body or "", ns, chash)
            total += 1                    # transaction committed → row done
        except Exception:
            # Roll back (the `with` already did) and stop this pass; the row stays
            # pending for the next tick / another worker. Don't hot-loop on it.
            _log.exception("embed worker failed on a pending memory; left pending")
            break
    return total
