"""Re-embed the whole corpus under a (new) embedding model.

Changing ``MEMGRES_EMBED_MODEL`` / ``_DIM`` invalidates every stored vector, so
normal startup *refuses* to run (``SchemaMismatch``) rather than mix models. This
is the deliberate path to actually switch: it re-stamps the model, wipes the chunk
store, recreates it at the new dimension, flags every memory for re-chunking, and
(by default) drains them right away. Bodies and history are never touched — only
the derived vectors are rebuilt.

Run it as ``memgres-reembed`` after changing the model in the environment. It is
destructive to the vector index (not the data), so it asks for confirmation unless
``--yes`` (or ``MEMGRES_REEMBED_YES=1``) is given. Safe to re-run.
"""

from __future__ import annotations

import logging
import sys
from typing import Tuple

_log = logging.getLogger("memgres.reembed")


def reembed(cfg, *, embedder=None, drain_now: bool = True) -> Tuple[int, int]:
    """Rebuild the chunk index under ``cfg``'s embedding model. Returns
    ``(flagged, embedded)``. ``drain_now=False`` flags but leaves embedding to a
    running worker. ``embedder`` defaults to the one ``cfg`` describes (a test may
    inject a stub)."""
    import psycopg

    from .embed_worker import EmbedWorker
    from .embeddings import get_embedder
    from .schema import migrate
    from .vector.base import make_backend

    if embedder is None:
        embedder = get_embedder(cfg)
    if embedder is None:
        raise SystemExit("MEMGRES_EMBED_PROVIDER=none — nothing to embed.")

    conn = psycopg.connect(cfg.database_url or "")
    conn.autocommit = True
    with conn.cursor() as cur:
        # Fail fast instead of hanging if a live connection still holds a lock on
        # the vector table/collection — re-embed is a maintenance op, run it with
        # the workers stopped (or it will report the contention).
        cur.execute("SET lock_timeout = '30s'")
        cur.execute("SELECT embed_model, embed_dim FROM memgres_meta WHERE only_row")
        row = cur.fetchone()
    old = f"{row[0]}/{row[1]}" if row else "none"
    _log.info("re-embedding: %s → %s/%s (%s backend)",
              old, cfg.embed_model, cfg.embed_dim, cfg.vector_backend)

    # 1. Re-stamp the model/dim so migrate() accepts the change instead of failing.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE memgres_meta SET embed_provider=%s, embed_model=%s, embed_dim=%s "
            "WHERE only_row",
            (cfg.embed_provider, cfg.embed_model, cfg.embed_dim))

    # 2. Wipe the chunk store so it's rebuilt at the new dimension.
    if cfg.vector_backend == "qdrant":
        from .vector.qdrant import QdrantBackend
        QdrantBackend.drop_chunks_collection()
    else:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS memory_segment CASCADE")

    # 3. Recreate the store at the new dim (pgvector: migrate re-adds
    #    memory_segment; qdrant: make_backend._ensure creates the new collection).
    migrate(conn, cfg)
    backend = make_backend(cfg, embedder)

    # 4. Flag every bodied memory for re-chunking.
    with conn.cursor() as cur:
        cur.execute("UPDATE memory SET embed_pending=true "
                    "WHERE body IS NOT NULL AND body <> ''")
        flagged = cur.rowcount
    _log.info("flagged %d memories for re-embedding", flagged)

    embedded = 0
    if drain_now:
        worker = EmbedWorker(cfg, embedder, backend,
                             connect=lambda: psycopg.connect(cfg.database_url or ""))
        embedded = worker.drain_once()
        worker.stop()
        _log.info("re-embedded %d memories", embedded)
    else:
        _log.info("left %d flagged for a running worker to drain", flagged)
    conn.close()
    return flagged, embedded


def _confirmed(argv) -> bool:
    import os
    if "--yes" in argv or "-y" in argv:
        return True
    if os.environ.get("MEMGRES_REEMBED_YES", "").strip().lower() in ("1", "true", "yes"):
        return True
    if sys.stdin.isatty():
        return input("Re-embed the whole corpus? This wipes the vector index "
                     "(not your data) and rebuilds it. [y/N] ").strip().lower() == "y"
    return False


def main() -> None:  # pragma: no cover - entrypoint
    from .config import load

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not _confirmed(sys.argv[1:]):
        print("Aborted. Pass --yes (or MEMGRES_REEMBED_YES=1) to confirm.")
        raise SystemExit(1)
    cfg = load()
    drain_now = "--no-drain" not in sys.argv[1:]
    flagged, embedded = reembed(cfg, drain_now=drain_now)
    print(f"done: flagged={flagged} embedded={embedded}"
          + ("" if drain_now else " (a worker will drain the rest)"))


if __name__ == "__main__":  # pragma: no cover
    main()
