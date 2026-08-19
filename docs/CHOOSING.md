# Choosing how to run memgres

memgres is one package with several run modes. This page picks the mode for your
situation; [DEPLOYMENT.md](DEPLOYMENT.md) has the mechanics, and
[TENANCY.md](TENANCY.md) the auth model.

## Pick by situation

| Your situation | Run it as | Why |
|---|---|---|
| One process that owns its memory (a script, one agent, a notebook) | **Embedded library** — `pip install memgres`, use `Store` directly | No server, no ports; writes embed inline and are searchable the instant `write()` returns. |
| One user, but you want the HTTP/MCP surface (Cursor, Claude Desktop, your own loop) | **`docker compose up`** (root [`docker-compose.yml`](../docker-compose.yml)) | Postgres + HTTP (`:8080`) + MCP (`:8765`), schema auto-migrated. Nothing to build. |
| **More than one user / agent / machine** | **The Docker server — shared, not per-machine** | One server many clients connect to, each pinned to its own token. See below. |
| ~100+ clients, or high write volume | **Docker split topology** ([`deploy/docker-compose.yml`](../deploy/docker-compose.yml)) | Stateless API tier + separate embed-worker tier, each scaled independently. |

## Many users → run the Docker version (one shared server)

**The recommendation:** for anything beyond a single user, deploy memgres **once**
with Docker and have every client — every teammate, agent, or machine — connect to
that **one running server** over HTTP/MCP. Do **not** give each person their own
`pip install` / stdio process pointed at the database.

Why a shared server, not per-machine installs:

- **One connection pool, one embed worker.** N per-machine stdio processes each open
  their own pool *and* each poll the DB and embed on their own — work and connections
  multiply with clients. A shared server funnels everyone through one pool
  (`MEMGRES_POOL_SIZE`) and one worker (or a worker tier), so cost scales with load,
  not with headcount.
- **Central identity.** With `MEMGRES_KEY_MODE=managed` the server mints per-client
  tokens and namespaces; you add, scope, and revoke access in one place. Per-machine
  installs each carry their own config and can't be centrally governed.
- **One thing to upgrade and back up.** Migrations run on the server's startup, so
  updating the image + restarting *is* the deploy for everyone at once; `pg_dump` of
  the one database is the whole backup.
- **Consistent embedding model.** Everyone shares the same stamped model/dimension —
  no risk of one machine quietly configured to a different model.

Two Docker shapes, both "the Docker version":

1. **All-in-one server** — root [`docker-compose.yml`](../docker-compose.yml) with
   `MEMGRES_KEY_MODE=managed` (or `open`). One container serves the HTTP API and MCP;
   its in-process worker embeds off the request path. Right for a team or a handful of
   clients. Point each client at the shared URL and pin its token in the client config
   (header or env) — the agent never handles the secret. See
   [TENANCY.md](TENANCY.md) and README → *Isolation*.
2. **Split topology** — [`deploy/docker-compose.yml`](../deploy/docker-compose.yml):
   a stateless API tier that only flags writes plus a `memgres-worker` tier that
   embeds, scaled separately (`--scale api=N --scale worker=M`). Reach for this at
   ~100+ clients or when embedding throughput is the bottleneck. Full walkthrough in
   [DEPLOYMENT.md](DEPLOYMENT.md).

Start with (1); move to (2) when a single server's pool or embed throughput is the
limit — the client-facing contract (HTTP/MCP + a token) doesn't change.

## Related

- **How** to deploy each shape, sizing knobs, reliability, changing the model:
  [DEPLOYMENT.md](DEPLOYMENT.md).
- **Auth** — users, namespaces, tokens, per-client pinning: [TENANCY.md](TENANCY.md).
- **Embedding model** local vs cloud (at scale, prefer a cloud/GPU model — bge-m3 on
  CPU embeds in seconds and throttles a single worker): [EMBEDDINGS.md](EMBEDDINGS.md).
- **Vector backend** pgvector vs Qdrant: [BACKENDS.md](BACKENDS.md).
