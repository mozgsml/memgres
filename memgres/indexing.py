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
    """Clear ``embed_pending`` (and reset the retry counters) only if the body is
    still the one we embedded (guarded by content_hash). A concurrent edit changed
    the hash and re-set the flag, so leaving it set hands the row to the next
    pass. A success zeroes ``embed_attempts``/``embed_failed_at`` so a later
    legitimate edit re-embeds cleanly rather than inheriting a stale failure."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE memory SET embed_pending=false, embed_attempts=0, "
            "embed_failed_at=NULL "
            "WHERE id=%s AND content_hash=%s AND embed_pending",
            (memory_id, src))


def _record_failure(conn, cfg, memory_id: str) -> None:
    """Record a failed embed attempt in its OWN transaction (the embed tx rolled
    back). The claim then skips this row for a back-off window, and once it has
    failed ``embed_max_attempts`` times the claim drops it entirely — a dead
    letter, left flagged and logged, so one poison body can't wedge the queue."""
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memory SET embed_attempts=embed_attempts+1, "
                    "embed_failed_at=now() WHERE id=%s RETURNING embed_attempts",
                    (memory_id,))
                r = cur.fetchone()
        attempts = r[0] if r else 0
        if attempts >= cfg.embed_max_attempts:
            _log.error("embed: memory %s failed %d× — dead-lettered (still flagged, "
                       "out of rotation); investigate its body or the model",
                       memory_id, attempts)
        else:
            _log.warning("embed: memory %s failed (attempt %d/%d); backing off %.0fs",
                         memory_id, attempts, cfg.embed_max_attempts,
                         cfg.embed_retry_backoff_s)
    except Exception:
        _log.exception("embed: could not record failure for %s", memory_id)


def drain(conn, cfg, embedder, backend, limit: Optional[int] = None) -> int:
    """Embed eligible pending memories oldest-first until none remain. Returns the
    count successfully embedded.

    Claim-based, so MANY workers (separate memgres-worker containers, or several
    server replicas each with an in-process worker) drain the same queue without
    duplicating work: each row is claimed with ``FOR UPDATE SKIP LOCKED`` inside
    the transaction that embeds it, so a concurrent worker skips a row already
    being handled rather than embedding it again or blocking on it. The lock is
    held only for that one row's embed and released on commit; the flag is cleared
    in the same transaction, so a crash mid-embed rolls back and leaves the row
    pending for another pass (crash-safe).

    A row that FAILS to embed does not stop the pass and does not wedge the queue:
    its attempt count + failure time are recorded (``_record_failure``), the claim
    skips it for a back-off window, and after ``embed_max_attempts`` it drops out
    of rotation (a logged dead letter) so newer rows always make progress. Only a
    claim/connection-level error ends the pass (the next tick reconnects).

    ``limit`` caps how many rows this call embeds (default: everything eligible)."""
    total = 0
    while limit is None or total < limit:
        row = None
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, body, namespace, content_hash FROM memory "
                        "WHERE embed_pending AND embed_attempts < %s "
                        "AND (embed_failed_at IS NULL "
                        "     OR embed_failed_at < now() - make_interval(secs => %s)) "
                        "ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                        (cfg.embed_max_attempts, cfg.embed_retry_backoff_s))
                    row = cur.fetchone()
                if row is None:
                    break                # nothing eligible & unclaimed → done
                mid, body, ns, chash = row
                index_memory(conn, cfg, embedder, backend, str(mid),
                             body or "", ns, chash)
            total += 1                    # committed → row done
        except Exception:
            if row is None:               # the claim itself failed → connection issue
                _log.exception("embed worker: claim failed; ending this pass")
                break
            # A single row's embed failed: record it and SKIP FORWARD, so a poison
            # row never blocks the rest of the queue.
            _log.warning("embed worker: embedding memory %s failed", row[0])
            _record_failure(conn, cfg, str(row[0]))
            continue
    return total
