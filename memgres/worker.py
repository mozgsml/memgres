"""Standalone embed worker — drains ``embed_pending`` off a shared memgres DB.

Run this as its own process/container in a SPLIT deployment: the API/MCP servers
set ``MEMGRES_EMBED_DISPATCH=async`` + ``MEMGRES_EMBED_WORKER=off`` (they only
flag writes, never embed on the request path), and one or more ``memgres-worker``
processes do the embedding. Claim-based draining (``FOR UPDATE SKIP LOCKED``)
makes running several worker replicas safe — each row is embedded once.

In an all-in-one deployment you don't need this: the server runs the worker
in-process (``MEMGRES_EMBED_WORKER=on``, the default).
"""

from __future__ import annotations

import logging
import signal

_log = logging.getLogger("memgres.worker")


def main() -> None:  # pragma: no cover - entrypoint
    import psycopg

    from .config import load
    from .embed_worker import EmbedWorker
    from .embeddings import get_embedder
    from .schema import migrate
    from .vector.base import make_backend

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    embedder = get_embedder(cfg)
    backend = make_backend(cfg, embedder)
    if embedder is None or backend is None:
        _log.warning("MEMGRES_EMBED_PROVIDER=%s → no embedder/backend configured; "
                     "nothing to embed. Exiting.", cfg.embed_provider)
        return

    # Ensure the schema is present/current before draining. The startup version
    # guard also refuses to run a worker older than the database's floor.
    with psycopg.connect(cfg.database_url or "") as c:
        migrate(c, cfg)

    worker = EmbedWorker(cfg, embedder, backend,
                         connect=lambda: psycopg.connect(cfg.database_url or ""))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_a: worker.stop())

    _log.info("memgres-worker draining %s (%s, dim %s) every %.1fs — Ctrl-C to stop",
              cfg.vector_backend, cfg.embed_model or cfg.embed_provider,
              cfg.embed_dim, cfg.embed_worker_interval)
    worker.serve()          # blocks until a signal calls stop()
    _log.info("memgres-worker stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
