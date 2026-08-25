"""Rebuild the link index from body text.

Edges are derived on write, so a corpus that existed before the link graph would
upgrade into a graph that is perfectly EMPTY — and `memory_links` would answer
"nothing points here" for every memory, which reads like a fact rather than like
"not indexed yet". So the backfill runs once, automatically, and the flag that
says it has run lives in `memgres_meta.links_built`.

Also available as ``memgres-relink`` for a forced rebuild — the same shape as
``memgres-reembed``, and for the same reason: an index derived from stored text
must be reconstructible without touching the text.
"""

from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger("memgres.relink")

BATCH = 500


class _NoEmbedder:
    """Stands in for an embedder so `Store` does not build the real one. The
    backfill never embeds; it re-reads text that is already stored."""
    dim = 0

    def embed_documents(self, texts):        # pragma: no cover - never called
        raise RuntimeError("the link backfill does not embed")

    embed_query = embed_documents


class _NoBackend:
    """Truthy stand-in so `Store` skips `make_backend`. Any call is a bug."""

    def __getattr__(self, name):             # pragma: no cover - never called
        raise RuntimeError(f"the link backfill does not use vectors ({name})")


_NO_BACKEND = _NoBackend()


def rebuild(conn, cfg, *, force: bool = False) -> int:
    """Re-derive every memory's outgoing edges. Returns how many memories were
    scanned; 0 when the backfill has already run and `force` is not set."""
    from .store import Store
    with conn.cursor() as cur:
        cur.execute("SELECT links_built FROM memgres_meta")
        row = cur.fetchone()
        if row and row[0] and not force:
            return 0

    # `_sync_links` touches neither embeddings nor vectors; passing a
    # sentinel backend keeps `Store` from building a qdrant client (or loading a
    # local embedding model) for a pass that only parses text.
    store = Store(cfg, embedder=_NoEmbedder(), conn=conn, backend=_NO_BACKEND)
    scanned = 0
    last = "00000000-0000-0000-0000-000000000000"
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, namespace, body FROM memory "
                "WHERE id > %s ORDER BY id LIMIT %s", (last, BATCH))
            rows = cur.fetchall()
        if not rows:
            break
        with conn.transaction(), conn.cursor() as cur:
            for mid, ns, body in rows:
                store._sync_links(cur, ns, mid, body)
        scanned += len(rows)
        last = rows[-1][0]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE memgres_meta SET links_built = true")
    _log.info("link index rebuilt over %d memories", scanned)
    return scanned


def maybe_backfill(cfg, connect) -> Optional[int]:
    """Run the one-time backfill if it has not run. Best effort: a failure here
    must not stop a server from starting, but it MUST be loud — a quietly empty
    link graph is the failure this exists to prevent."""
    try:
        conn = connect()
    except Exception:
        _log.exception("link backfill could not connect; link graph may be empty")
        return None
    try:
        return rebuild(conn, cfg)
    except Exception:
        _log.exception("link backfill failed; link graph may be incomplete until "
                       "`memgres-relink` is run")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> None:  # pragma: no cover - entrypoint
    import psycopg
    from .config import load
    from .schema import migrate

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    with psycopg.connect(cfg.database_url or "") as conn:
        migrate(conn, cfg)
        n = rebuild(conn, cfg, force=True)
        conn.commit()
    print(f"link index rebuilt over {n} memories")
