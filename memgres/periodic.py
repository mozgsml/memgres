"""Background tasks that run on a timer, and the lifecycle they share.

:class:`PeriodicWorker` is the machinery: one daemon thread, one dedicated
connection, a stop event, and a tick that never lets an exception kill the loop.
:class:`EmbedWorker` and :class:`RetentionSweeper` differ only in what one tick
does, so the lifecycle lives here once rather than being re-implemented — and a
future sweep (dangling links, staleness) is a subclass, not another copy.

Why the retention sweep is NOT part of the embed worker, though it would have
been fewer lines: the embed worker only exists when there is an embedder and a
vector backend (:func:`~memgres.embed_worker.maybe_start_worker`). A deployment
that keeps no vectors would then quietly keep expired data forever — a retention
promise failing silently because an unrelated feature is off. The two run on
independent conditions because they answer to independent policies.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

_log = logging.getLogger("memgres.periodic")


class PeriodicWorker:
    """One background task: ``_tick()`` on ``interval``, until :meth:`stop`.

    Subclasses implement ``_tick`` and set ``name``. The connection is lazy and
    self-healing: a tick that raises drops it, so a database restart costs one
    failed pass rather than a dead thread.
    """

    name = "memgres-periodic"

    def __init__(self, cfg, connect: Callable[[], "object"], interval: float):
        self.cfg = cfg
        self._connect = connect          # () -> a fresh psycopg connection
        self._interval = interval
        self._conn = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ─── subclass contract ──────────────────────────────────────────────────
    def _tick(self) -> None:
        raise NotImplementedError

    # ─── connection ─────────────────────────────────────────────────────────
    def _conn_ok(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = self._connect()
        return self._conn

    def _reset_conn(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    # ─── loop ───────────────────────────────────────────────────────────────
    def _run(self) -> None:
        # The first iteration runs IMMEDIATELY, before the first sleep: on a
        # restart that is the catch-up pass (rows left pending, expiries that
        # came due while the process was down).
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                _log.exception("%s tick failed; dropping connection, retrying",
                               self.name)
                self._reset_conn()
            self._stop.wait(self._interval)

    def start(self) -> "PeriodicWorker":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name=self.name,
                                        daemon=True)
        self._thread.start()
        return self

    def serve(self) -> None:
        """Run the loop in the CURRENT thread, blocking until :meth:`stop`. Used
        by standalone worker processes (a signal handler calls ``stop()``); an
        in-process server uses :meth:`start` instead."""
        self._run()
        self._reset_conn()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._reset_conn()


class RetentionSweeper(PeriodicWorker):
    """Deletes memories whose retention window has closed.

    Reads hide expired rows already (``build_filters``), so nothing here changes
    what a search returns. What it changes is whether the data is still HELD —
    which is the only thing a retention promise is actually about.
    """

    name = "memgres-retention"

    def __init__(self, cfg, connect: Callable[[], "object"],
                 embedder=None, backend=None,
                 interval: Optional[float] = None):
        super().__init__(cfg, connect,
                         interval if interval is not None
                         else cfg.retention_sweep_interval)
        # Both are passed in rather than left to default: `Store` builds an
        # embedder when given none, and for a local provider that loads the model
        # — a sweep that deletes rows has no use for one, and the server already
        # holds both.
        self._embedder = embedder
        self._backend = backend

    def sweep_once(self) -> int:
        """One sweep; returns how many memories were deleted. The loop calls
        this, and so can a test or a CLI."""
        from .store import Store
        store = Store(self.cfg, embedder=self._embedder, conn=self._conn_ok(),
                      backend=self._backend)
        n = store.purge_expired()
        if n:
            _log.info("retention sweep deleted %d expired memories", n)
        self._sweep_search_log()
        return n

    def _sweep_search_log(self) -> int:
        """Expire search-log rows on their own clock.

        The log is not memories, so it does not follow `MEMGRES_RETENTION_DAYS`
        — a deployment that keeps memories forever still should not keep a
        record of every question anyone asked. Best-effort, like the log itself.
        """
        # NOT gated on `search_log` being on: the documented workflow is to turn
        # it on, gather cases, and turn it back off — and gating the sweep on the
        # toggle would mean every query gathered that way is then kept forever.
        # The DELETE is a no-op where nothing was ever logged.
        days = self.cfg.search_log_days
        if days <= 0:
            return 0
        try:
            conn = self._conn_ok()
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("DELETE FROM search_log WHERE at < now() - %s::interval",
                            (f"{days} days",))
                n = cur.rowcount or 0
        except Exception as e:                                  # pragma: no cover
            _log.warning("search log sweep failed (ignored): %s", e)
            return 0
        if n:
            _log.info("search log sweep deleted %d rows older than %d days", n, days)
        return n

    def _tick(self) -> None:
        self.sweep_once()


def _log_has_rows(connect: Callable[[], "object"]) -> bool:
    """Is there anything left in the search log? Asked once, at startup, and any
    failure answers "no" — this decides whether to start a housekeeping thread,
    and a housekeeping question must never keep a server from coming up."""
    try:
        conn = connect()
    except Exception:                                           # pragma: no cover
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM search_log)")
            return bool(cur.fetchone()[0])
    except Exception:                                           # pragma: no cover
        return False
    finally:
        try:
            conn.close()
        except Exception:                                       # pragma: no cover
            pass


def maybe_start_sweeper(cfg, connect: Callable[[], "object"],
                        embedder=None, backend=None) -> Optional[RetentionSweeper]:
    """Start a :class:`RetentionSweeper` when the deployment actually has a
    retention policy. ``retention_days <= 0`` means "keep everything", and then
    there is nothing to sweep — no thread, no connection held open for a table
    scan that can never match.

    ``MEMGRES_RETENTION_SWEEP=false`` turns it off for THIS process. Every server
    process starts one otherwise, and in a stdio-MCP deployment a process is a
    client session — so a dozen sessions would mean a dozen threads issuing the
    same deployment-wide DELETE. They do not corrupt anything (the delete is
    bounded and idempotent, and each row can only be deleted once), but the work
    is redundant, so a deployment that runs a dedicated sweeper can silence the
    rest."""
    # A deployment can keep memories forever and still expire the search log,
    # so either policy is reason enough to run the thread. The log side asks a
    # question rather than reading the toggle: the documented workflow is to
    # turn logging ON, gather cases, and turn it OFF again, and rows gathered
    # that way still have to expire. So: is the log on, or is there anything
    # left in it? A deployment that never logged starts no thread.
    sweeps_log = cfg.search_log_days > 0 and (cfg.search_log or _log_has_rows(connect))
    if (cfg.retention_days <= 0 and not sweeps_log) or not cfg.retention_sweep:
        return None
    return RetentionSweeper(cfg, connect, embedder, backend).start()
