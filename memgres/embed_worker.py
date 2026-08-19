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
            # Bound how long a single embed transaction may sit between statements
            # (i.e. while the embed call runs): if it hangs, Postgres kills the tx
            # and releases the SKIP-LOCKED row lock, so the row is retried and
            # never stuck. Autocommit so this SET isn't itself inside a tx.
            if self.cfg.embed_tx_timeout_ms > 0:
                try:
                    prev = self._conn.autocommit
                    self._conn.autocommit = True
                    with self._conn.cursor() as cur:
                        cur.execute("SET idle_in_transaction_session_timeout = %s",
                                    (self.cfg.embed_tx_timeout_ms,))
                    self._conn.autocommit = prev
                except Exception:
                    _log.exception("could not set idle_in_transaction timeout")
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

    def serve(self) -> None:
        """Run the drain loop in the CURRENT thread, blocking until ``stop()``.
        Used by the standalone ``memgres-worker`` process (a signal handler calls
        ``stop()``); the in-process server path uses ``start()`` instead."""
        self._run()
        self._reset_conn()

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
    backend ONCE, start the in-process embed worker if warranted, and return
    ``(worker, cfg, backend)`` with ``cfg.embed_dispatch`` set to what actually
    holds. The caller injects ``backend`` into every per-request ``Store`` so a
    qdrant client isn't rebuilt each call.

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
    worker = maybe_start_worker(
        cfg, embedder, backend,
        connect=lambda: psycopg.connect(cfg.database_url or ""))
    dispatch = "async" if worker is not None else cfg.embed_dispatch
    return worker, replace(cfg, embed_dispatch=dispatch), backend
