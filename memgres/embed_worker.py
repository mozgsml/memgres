"""Background embed worker: builds chunk vectors off the write path.

When a server runs this worker, writes only flag ``embed_pending`` and return
immediately; the worker drains those rows — segment, embed, index — on its own
connection and thread. That's what keeps a write fast even for a large body (the
embedding no longer runs inside the request).

One daemon thread, one dedicated connection. It backfills on start (so a restart
catches up any rows left pending, including the one-time re-chunk after the
schema upgrade), then polls. ``drain_once`` is the same code the loop runs and is
directly callable from a test or a CLI. The real work lives in
:func:`memgres.indexing.drain`; this is just its lifecycle.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from .indexing import drain

_log = logging.getLogger("memgres.embed_worker")


class EmbedWorker:
    def __init__(self, cfg, embedder, backend,
                 connect: Callable[[], "object"]):
        self.cfg = cfg
        self.embedder = embedder
        self.backend = backend
        self._connect = connect          # () -> a fresh psycopg connection
        self._conn = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _conn_ok(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = self._connect()
        return self._conn

    def drain_once(self) -> int:
        """One synchronous drain pass over all currently-pending rows. Returns the
        count embedded. Used by the loop and directly by tests."""
        return drain(self._conn_ok(), self.cfg, self.embedder, self.backend)

    def _run(self) -> None:
        # The first iteration IS the backfill (catch up rows left pending across a
        # restart / the schema upgrade). Done in the thread, not in start(), so
        # building a server never blocks on embedding a backlog.
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:
                _log.exception("embed worker drain failed; dropping connection, retrying")
                self._reset_conn()
            self._stop.wait(self.cfg.embed_worker_interval)

    def _reset_conn(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def start(self) -> "EmbedWorker":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="memgres-embed",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._reset_conn()


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
    """Server-side setup shared by the HTTP and MCP entrypoints: build the vector
    backend, start the embed worker if warranted, and return ``(worker, cfg)``
    where ``cfg.embed_async`` is set to match — True **iff** a worker is running.

    Tying async to the worker's existence is the safety rail: with a worker,
    writes defer to it (fast); without one, writes stay synchronous (embed
    inline), so a deployment never ends up flagging rows that nothing will ever
    embed (a silent semantic gap)."""
    import psycopg
    from dataclasses import replace

    from .vector.base import make_backend

    backend = make_backend(cfg, embedder)
    worker = maybe_start_worker(
        cfg, embedder, backend,
        connect=lambda: psycopg.connect(cfg.database_url or ""))
    return worker, replace(cfg, embed_async=worker is not None)
