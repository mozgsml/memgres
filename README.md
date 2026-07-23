# memgres

**Versioned document memory for AI agents — one Postgres, lexical *or* semantic recall, diff-based history, GDPR-erasable.**

> Status: early. Core library, search, HTTP API and MCP server are implemented and tested against a live pgvector Postgres.

memgres is a lightweight, drop-in memory layer — a Python library plus an optional HTTP/MCP service — backed by a single PostgreSQL database. You store **documents** (bodies of text an agent owns and edits), not facts an LLM guessed at. Every change is an authored diff with provenance, kept in a tamper-evident history that you can still delete when the law says you must.

---

## Why memgres exists

memgres is a **document** store where **you** own the write path — not a fact store that lets a model decide what to remember for you.

- You write the **whole body** or a **unified diff** — nothing re-interprets your text on the way in.
- Concurrency is guarded by **content hash** (optimistic locking): you send the hash of the body you edited; the write applies only if the current body still matches, otherwise you get a `409` and re-read. No lost updates, no silent overwrites.
- Every change is stored as a **hash-chained unified diff** with `source`/`reason` provenance — git-like history you can replay and attribute line by line.
- **History rows are deletable**: real GDPR erasure, not "hidden from results but still on disk".

Reach for memgres when you want **auditable, authored, versioned text memory**.

---

## Advantages

| Advantage | Why it matters |
|---|---|
| **No LLM on the write path** | Writes are instant and free; nothing invents, summarizes, or drops your content behind your back. |
| **Authored unified-diff writes** | You control exactly what changes; the diff *is* the audit record. |
| **Content-hash optimistic concurrency (409)** | Concurrent writers can't silently clobber each other — a stale write is rejected, not merged blind. |
| **Postgres on disk** | The corpus can far exceed RAM, and concurrent writes are safe — no in-memory bound, no single-writer lock. |
| **Hash-chained, GDPR-deletable history** | Tamper-evident provenance you can *still* erase: `forget()` hard-deletes the row, its vectors, and crypto-shreds the chain — no ["ghost vectors" left reconstructible in the index](https://arxiv.org/pdf/2606.18497). |
| **Lexical works with zero embeddings** | Deploy with no model, no API, no GPU — Postgres full-text search out of the box. Turn on semantic recall only when you want it. |
| **Lexical *and* semantic (hybrid)** | Exact identifiers/codes go to lexical (where [dense retrieval alone stumbles](https://tianpan.co/blog/2026-04-12-hybrid-search-production-bm25-dense-embeddings)); meaning-based queries go to vectors; hybrid fuses both with RRF. |
| **Embedding-model safety by construction** | The model id + dimension are stamped into the schema; a mismatch **hard-fails** instead of silently returning garbage. |
| **TTL renewed on read** | Active memory persists because it's used; abandoned memory expires itself. Storage self-cleans instead of growing forever. |
| **Optional token namespaces** | Multi-tenant isolation when you need it (secret token → namespace); nothing to configure for single-user. |
| **Fast subtree recall via `ltree`** | Memories form a real tree; `path <@ 'a.b'` pulls a whole subtree in one GiST index scan, no recursive walk that degrades with depth. |
| **Git-blame + version reconstruct** | Every line carries who last changed it (grouped into author-blocks); any past version reconstructs from history — no replaying diffs yourself. |
| **One Postgres, one backup** | The whole thing is `pg_dump`-able; the vector index rebuilds from the source of truth. No second datastore to run or back up. |
| **Drop-in module, not a framework** | `pip install`, or `docker compose up`, or point at your own Postgres. No platform to adopt. |

---

## Design at a glance

```
memgres (core library, no HTTP dependency)
  ├─ store        write (whole body OR unified diff) · get · recall · move · forget
  ├─ diffing      unified diff make/apply + content hash (optimistic locking)
  ├─ blame        line-attributed document + reconstruct any past version
  ├─ organize     tags (text[] + GIN) · tree (ltree path + GiST, fast subtree select)
  ├─ search       lexical (Postgres FTS)  +  semantic (pgvector; Qdrant planned)  +  hybrid
  ├─ embeddings   provider via env: none | local (sentence-transformers) | cloud (Jina/OpenAI)
  └─ config       every limit via env (body/write size, TTL, namespaces, …)

optional layers on top of the same core:
  ├─ HTTP API (FastAPI)   REST + OpenAPI
  └─ MCP server           write/recall/get/blame/move/forget as MCP tools
```

**Record model:** one memory = one mutable body (up to a configurable ceiling, default 256 KB) plus metadata — `tags` (cross-cutting labels, `text[]` + GIN), a `path` (its place in an `ltree` tree), timestamps, and per-diff provenance (`source`/`reason`, kept in history). A single write/diff is capped smaller (default 16 KB), so large bodies accrue over many authored diffs. **Organization is two orthogonal axes:** the tree is *where a memory lives* (one place, subtree-selectable); tags are *what it's about* (many, overlapping). Both filter either search — narrow a semantic query to a subtree, or list a tag across the tree.

**Isolation:** optional token *namespaces* keep tenants from seeing each other's memories (namespace = hash of a secret token, so one wallet can back many clients). Encryption at rest is left to the deployment — Postgres/managed-PG/disk TDE stays transparent to queries, so search keeps working; memgres deliberately does **not** encrypt bodies application-side (that would make them unsearchable, which is why no comparable tool does it either). GDPR erasure is real: `forget()` hard-deletes the row, its vectors, and crypto-shreds the history chain. All limits are env-configurable, so the same code serves a single-user embed and a capped multi-tenant service.

---

## Quickstart

**The whole thing, one command** — brings up `pgvector` + the memgres service on `http://localhost:8080`, schema auto-migrated on startup:

```bash
docker compose up            # → http://localhost:8080  (GET /healthz → {"ok":true})
```

```bash
# create a memory
curl -sX POST localhost:8080/memories \
  -H 'content-type: application/json' \
  -d '{"body":"Postgres tuning notes\nshared_buffers = 25% RAM\n","tags":["db"],"path":"ops.postgres","source":"me"}'
# → {"id":"…","content_hash":"…","seq":1, …}

# recall (lexical out of the box; semantic once you set an embedding provider)
curl -s 'localhost:8080/recall?q=postgres%20tuning'

# edit by unified diff, guarded by the hash you edited (409 if stale)
curl -sX PATCH localhost:8080/memories/$ID \
  -H 'content-type: application/json' \
  -d '{"diff":"--- \n+++ \n@@ -2 +2 @@\n-shared_buffers = 25% RAM\n+shared_buffers = 40% RAM\n","base_hash":"'$HASH'","source":"me","reason":"bump"}'

# who wrote each line (grouped into author-blocks by default)
curl -s localhost:8080/memories/$ID/blame
```

### As a Python library (no HTTP)

```python
from memgres import Store, load_config, migrate
import psycopg

cfg  = load_config()                       # reads MEMGRES_* env
conn = psycopg.connect(cfg.database_url)
migrate(conn, cfg)                         # idempotent; stamps embed model/dim

s = Store(cfg, conn=conn)
m = s.write(body="remember this\n", tags=["note"], path="misc.reminder", source="me")

# edit: whole body OR a diff carrying the base hash (optimistic concurrency)
m = s.write(id=m.id, body="remember this, updated\n", base_hash=m.content_hash, reason="tweak")

hits  = s.recall(None, "what did I remember?", k=5)          # lexical / semantic / hybrid / auto
blame = s.annotate_grouped(None, m.id)                        # [{start,end,source,reason,…}]
old   = s.reconstruct(None, m.id, 1)                          # body as of version 1
s.forget(None, m.id)                                          # hard-erase + history
```

### Three ways to run it

1. **`docker compose up`** — `pgvector` + service, nothing to configure. (A `qdrant` compose profile and `MEMGRES_VECTOR_BACKEND=qdrant` flag are scaffolded but the Qdrant backend isn't wired yet — pgvector is the working path.)
2. **Your own Postgres** — `pip install "memgres[server]"`, point `MEMGRES_DATABASE_URL` at it, run `memgres-server` (migrates on startup).
3. **Embedded library** — `pip install memgres`, use `Store` directly, no HTTP at all.

Semantic recall is optional: the default `MEMGRES_EMBED_PROVIDER=none` gives you lexical FTS with zero models. Turn on `local` (sentence-transformers) or a cloud API (`jina`/`openai`) when you want meaning-based search — the model id + dimension get stamped into the schema and a later mismatch hard-fails instead of silently returning garbage.

## Configuration

Everything is env, all optional (defaults suit a single-user embed). Full list in [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `MEMGRES_DATABASE_URL` | libpq env | Postgres connection string |
| `MEMGRES_MAX_BODY_BYTES` | `262144` | ceiling for a whole record body (256 KB) |
| `MEMGRES_MAX_WRITE_BYTES` | `16384` | ceiling for one write/diff payload (≤ body) |
| `MEMGRES_RETENTION_DAYS` | `0` | `0` = keep forever; `>0` = expire N days after last touch |
| `MEMGRES_RENEW_ON_READ` | `true` | a read pushes the expiry clock forward |
| `MEMGRES_NAMESPACES` | `false` | `true` = each caller sends a secret token; namespace = its hash |
| `MEMGRES_TREE` | `true` | `ltree` path column + GiST index (fast subtree select) |
| `MEMGRES_REQUIRE_PARENT` | `false` | `true` = a node's parent path must already exist |
| `MEMGRES_HISTORY` | `true` | keep the hash-chained diff history (deleted with the record) |
| `MEMGRES_FTS_LANGUAGE` | `simple` | Postgres FTS dictionary (`simple`/`english`/…) |
| `MEMGRES_VECTOR_BACKEND` | `pgvector` | `pgvector` (same DB); `qdrant` scaffolded, not yet wired |
| `MEMGRES_EMBED_PROVIDER` | `none` | `none` / `local` / `jina` / `openai` |
| `MEMGRES_EMBED_MODEL` / `_DIM` / `_API_KEY` / `_API_BASE` | — | embedding model settings |

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/memories` | create |
| `GET` | `/memories/{id}` | read (renews TTL) |
| `PATCH` | `/memories/{id}` | edit: whole `body` **or** `diff`+`base_hash`; move; retag |
| `POST` | `/memories/{id}/move` | reparent a node (cascades its subtree) |
| `DELETE` | `/memories/{id}` | forget (hard-erase + history) |
| `GET` | `/memories/{id}/history` | raw change chain |
| `GET` | `/memories/{id}/blame` | line attribution; `?group`, `?text`, `?lines=1,3-5` |
| `GET` | `/memories/{id}/at/{seq}` | body reconstructed at a version |
| `GET` | `/recall` | `?q=&k=&mode=&tags=&path_prefix=` |
| `GET` | `/healthz` | liveness |

Namespace token (when `MEMGRES_NAMESPACES=true`) goes in `Authorization: Bearer <token>` or `X-Memgres-Token`. OpenAPI/Swagger is served at `/docs`. Store errors map to status codes: `409` stale-hash conflict, `404` not found, `413` too large, `401` missing token.

## MCP server

The same store is exposed to MCP clients (Claude Desktop, etc.) over stdio:

```bash
pip install "memgres[mcp]"
memgres-mcp                    # needs MEMGRES_DATABASE_URL; migrates on startup
```

Tools: `memory_write` (create or edit by body/diff), `memory_get`, `memory_recall`, `memory_blame`, `memory_history`, `memory_move`, `memory_forget`. Point your MCP client's config at the `memgres-mcp` command.

---

## License

MIT — see [LICENSE](LICENSE). Fully self-hostable, no gated features.
