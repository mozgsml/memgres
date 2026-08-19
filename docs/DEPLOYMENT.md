# Deploying memgres

memgres is one Python package with a few entrypoints. How you run them decides
the topology — from a single embedded process to a horizontally-scaled service.

## Topologies

### 1. Embedded / library (no server)

Import `memgres.store.Store` directly. Writes embed **inline**
(`MEMGRES_EMBED_DISPATCH=inline`, the default), so a memory is semantically
searchable the instant `write()` returns — no worker, no moving parts. Best for a
single process that owns its memory.

### 2. All-in-one server (stdio MCP or one HTTP instance)

Run `memgres-mcp` (stdio, one client) or `memgres-server` (HTTP). The process
runs an **in-process embed worker** (`MEMGRES_EMBED_WORKER=on`, default): writes
return fast (flag `embed_pending`), the worker embeds off the request path. This
is the right shape for an operator, a small team, or a handful of clients.

### 3. Split service (enterprise, many clients)

For ~100+ clients, separate the tiers — see `deploy/docker-compose.yml`:

```
        clients ──HTTP──▶  api ×N  (memgres-server)     only FLAG writes
                              │      EMBED_DISPATCH=async, EMBED_WORKER=off
                              ▼
                          Postgres  ◀── worker ×M  (memgres-worker)   embed
                          Qdrant    ◀──/            drains the queue
```

- **API tier** (`memgres-server`, scale to N replicas): serves HTTP, resolves each
  client's identity from its `Authorization: Bearer <token>` (managed mode), and
  only **flags** writes (`MEMGRES_EMBED_DISPATCH=async`, `MEMGRES_EMBED_WORKER=off`).
  No embedding on the request path → fast, light, stateless.
- **Worker tier** (`memgres-worker`, scale to M replicas): drains `embed_pending`
  and embeds. **Claim-based** (`FOR UPDATE SKIP LOCKED`) so replicas never embed
  the same memory twice and never block each other.
- **Postgres** holds bodies, tree, history (source of truth). **Qdrant** holds the
  chunk vectors. One connection pool per API/worker process (`MEMGRES_POOL_SIZE`),
  not one per client — put pgbouncer in front only if you outgrow Postgres
  connections.

Why this scales where per-client stdio doesn't: N clients share one pool and a
fixed worker pool, instead of N processes each polling the DB and embedding.

## Sizing & knobs

| Concern | Knob |
|---|---|
| Concurrent clients per API instance | `MEMGRES_POOL_SIZE` (raise from 4 → 20–50) |
| Embedding throughput | worker replicas + a fast embedder (cloud, or local GPU) |
| Worker poll cadence | `MEMGRES_EMBED_WORKER_INTERVAL` (s) |
| Poison-row handling | `MEMGRES_EMBED_MAX_ATTEMPTS` / `_RETRY_BACKOFF_S` (retry, then dead-letter) |
| Chunk size / overlap | `MEMGRES_CHUNK_CHARS` (400), `MEMGRES_CHUNK_OVERLAP` (80) |

**Embedder:** at scale prefer a **cloud** model (`MEMGRES_EMBED_PROVIDER=openai`
/`jina`/`openai-compatible`) — sub-second, no GPU in your containers, light image.
A local `sentence-transformers` model works but per-embed latency in seconds
throttles a single worker; add replicas or a GPU. See `docs/EMBEDDINGS.md`.

## Reliability

- **Crash-safe embedding.** A write only flags `embed_pending`; the worker embeds
  and clears the flag in one transaction. A crash mid-embed rolls back → the row
  stays flagged and another worker (or the next restart's backfill) picks it up.
  Nothing is lost, and a row is never "locked forever": the claim lock lives with
  the DB connection and is released when it dies (power cut, kill). A slow/stalled
  embed is bounded by the embedder's own request timeout, not a DB-side kill.
- **One poison row can't wedge the queue.** A row that keeps failing to embed is
  retried after a back-off and, after `MEMGRES_EMBED_MAX_ATTEMPTS`, dead-lettered
  (left flagged, out of the claim rotation, logged) — newer rows always progress.
- **Version guard.** On connect, a client refuses to run below the database's
  recorded compatibility floor (a newer memgres migrated it past a breaking
  change) with an actionable "update this client" error — so a stale replica can't
  silently misread a migrated store. Upgrade the fleet together across a breaking
  change; additive changes let old and new coexist.
- **Migrations apply on startup.** There is no separate migrate step: each
  `memgres-server`/`memgres-mcp`/`memgres-worker` runs the (idempotent) migration
  on start. Updating the package + restarting IS the deploy.

## Changing the embedding model — `memgres-reembed`

Switching `MEMGRES_EMBED_MODEL`/`_DIM` invalidates every stored vector, so normal
startup refuses (rather than mix models). To switch deliberately:

1. Stop the workers (and ideally the API, or run in a maintenance window).
2. Set the new `MEMGRES_EMBED_MODEL`/`_DIM` in the environment.
3. Run `memgres-reembed --yes`. It re-stamps the model, wipes + recreates the
   chunk store at the new dimension, flags every memory, and rebuilds. Bodies and
   history are untouched. `--no-drain` flags only and leaves embedding to a running
   worker.
4. Bring the tiers back up.

## Backup

Postgres is the source of truth (bodies, tree, history); the Qdrant/pgvector
vectors are derived and rebuildable (`memgres-reembed`, or the startup backfill).
So a `pg_dump` of the memgres database is a complete backup — restore it and let a
worker re-embed if the vector store is gone.
