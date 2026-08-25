"""Background embed worker: builds chunk vectors off the write path.

When a server runs this worker, writes only flag ``embed_pending`` and return
immediately; the worker drains those rows — segment, embed, index — on its own
connection and thread. That's what keeps a write fast even for a large body (the
embedding no longer runs inside the request).

One daemon thread, one dedicated connection. It backfills on start (so a restart
catches up any rows left pending, including the one-time re-chunk after the
schema upgrade), then polls. ``drain_once`` is the same code the loop runs and is
directly callable from a test or a CLI. The real work lives in
:func:`memgres.indexing.drain`; the lifecycle lives in
:class:`memgres.periodic.PeriodicWorker`, shared with the retention sweep.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .indexing import drain
from .periodic import PeriodicWorker, maybe_start_sweeper

_log = logging.getLogger("memgres.embed_worker")


class EmbedWorker(PeriodicWorker):
    """Drains ``embed_pending`` — one tick is one drain pass.

    The first tick IS the backfill (rows left pending across a restart or the
    schema upgrade). It happens in the thread rather than in ``start()``, so
    building a server never blocks on embedding a backlog."""

    name = "memgres-embed"

    def __init__(self, cfg, embedder, backend,
                 connect: Callable[[], "object"]):
        super().__init__(cfg, connect, cfg.embed_worker_interval)
        self.embedder = embedder
        self.backend = backend

    def drain_once(self) -> int:
        """One synchronous drain pass over all currently-pending rows. Returns the
        count embedded. Used by the loop and directly by tests."""
        return drain(self._conn_ok(), self.cfg, self.embedder, self.backend)

    def _tick(self) -> None:
        self.drain_once()

    def stop(self) -> None:
        self._stop.set()
def maybe_start_worker(cfg, embedder, backend,
                       connect: Callable[[], "object"]) -> Optional[EmbedWorker]:
    """Start an :class:`EmbedWorker` when there's something to embed and the
    deployment wants one (``MEMGRES_EMBED_WORKER``, default on). Returns the
    running worker, or ``None`` — in which case the caller must keep writes
    synchronous (embed inline), so semantic recall never silently lags."""
    if embedder is None or backend is None or not cfg.embed_worker:
        return None
    return EmbedWorker(cfg, embedder, backend, connect).start()


def wire_server(cfg, embedder):
    """Server-side background setup shared by the HTTP and MCP entrypoints: build
    the vector backend ONCE, start the in-process embed worker if warranted, start
    the retention sweep if the deployment has a retention policy, and return
    ``(worker, cfg, backend)`` with ``cfg.embed_dispatch`` set to what actually
    holds. The caller injects ``backend`` into every per-request ``Store`` so a
    qdrant client isn't rebuilt each call.

    The sweep is started here and not returned: like the embed worker at both
    call sites, nothing holds it — it is a daemon thread that dies with the
    process. Drive it directly (``RetentionSweeper.sweep_once``) in a test.

    Dispatch resolution:
      * an in-process worker started (``embed_worker`` on, embedder present) →
        writes defer to it → ``async``. The all-in-one server default: fast writes
        with a local drainer, never a flag nothing embeds.
      * no in-process worker → keep the operator's ``embed_dispatch``. That is how
        a SPLIT deployment works: an API container sets ``async`` + ``embed_worker
        =off`` and a separate ``memgres-worker`` drains. Left at the default
        (``inline``), a workerless server just embeds inline — safe, never a
        silent gap."""
    import psycopg
    from dataclasses import replace

    from .vector.base import make_backend

    backend = make_backend(cfg, embedder)
    connect = lambda: psycopg.connect(cfg.database_url or "")   # noqa: E731
    worker = maybe_start_worker(cfg, embedder, backend, connect=connect)
    # Retention answers to its own policy, not to whether embeddings are on: a
    # lexical-only deployment must still stop holding expired data.
    maybe_start_sweeper(cfg, connect, embedder, backend)
    dispatch = "async" if worker is not None else cfg.embed_dispatch
    if dispatch == "async" and worker is None and backend is not None:
        # async + no local worker: writes will flag embed_pending and this process
        # embeds nothing. Correct ONLY in a split deployment with a separate
        # memgres-worker. Warn loudly so a missing/failed worker isn't a silent gap.
        _log.warning("MEMGRES_EMBED_DISPATCH=async with no in-process worker: writes "
                     "will be flagged but NOT embedded here — a separate "
                     "memgres-worker MUST be running, or semantic recall will lag.")
    return worker, replace(cfg, embed_dispatch=dispatch), backend
