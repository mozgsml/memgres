# memgres

[![CI](https://github.com/mozgsml/memgres/actions/workflows/ci.yml/badge.svg)](https://github.com/mozgsml/memgres/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Versioned document memory for AI agents — one Postgres, lexical *or* semantic recall, diff-based history, GDPR-erasable.**

> Status: v0.2.0 — on [PyPI](https://pypi.org/project/memgres/) (`pip install memgres`) and [GHCR](https://ghcr.io/mozgsml/memgres). Core library, search (pgvector/Qdrant), multi-tenant identity (users / namespaces / scoped tokens), HTTP API and MCP server, all tested in CI against live Postgres + Qdrant.

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
| **Snippets, not walls of text** | Recall returns one body view per hit — never both a slice and the whole thing. Long hits come back as the most relevant slice (`kind="snippet"`) with its `lines` range; semantic hits pick their best segment (embedded once, then cached), lexical uses a clean `ts_headline` (no markup). A body short enough that a slice would just repeat it comes back whole (`kind="full"`). Pass `full_body=true` to force whole bodies. |
| **Embedding-model safety by construction** | The model id + dimension are stamped into the schema; a mismatch **hard-fails** instead of silently returning garbage. |
| **Optional TTL, renewed on read** | Off by default — memory is kept forever. Turn on a retention window and active memory persists because it's used, while abandoned memory expires itself: storage self-cleans instead of growing. |
| **Optional multi-tenant identity** | Users, namespaces and rotatable scoped tokens when you need isolation; nothing to configure for single-user. |
| **Fast subtree recall via `ltree`** | Memories form a real tree; `path <@ 'a.b'` pulls a whole subtree in one GiST index scan, no recursive walk that degrades with depth. |
| **Git-like blame + version reconstruct** | Every line carries who last changed it (grouped into author-blocks); any past version reconstructs from history — no replaying diffs yourself. |
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
  ├─ search       lexical (Postgres FTS)  +  semantic (pgvector or Qdrant)  +  hybrid
  ├─ embeddings   provider via env: none | local (sentence-transformers) | cloud (Jina/OpenAI)
  └─ config       every limit via env (body/write size, TTL, key mode, …)

optional layers on top of the same core:
  ├─ HTTP API (FastAPI)   REST + OpenAPI
  └─ MCP server           write/recall/get/blame/move/forget as MCP tools
```

**Record model:** one memory = one mutable body (up to a configurable ceiling, default 256 KB) plus metadata — `tags` (cross-cutting labels, `text[]` + GIN), a `path` (its place in an `ltree` tree), timestamps, and per-diff provenance (`source`/`reason`, kept in history). A single write/diff is capped smaller (default 16 KB), so large bodies accrue over many authored diffs. **Organization is two orthogonal axes:** the tree is *where a memory lives* (one place, subtree-selectable); tags are *what it's about* (many, overlapping). Both filter either search — narrow a semantic query to a subtree, or list a tag across the tree.

**Isolation:** optional multi-tenant identity keeps tenants from seeing each other's memories — users own *namespaces*, and rotatable, permission-scoped *tokens* authenticate as a user (turned on with `MEMGRES_KEY_MODE=open|managed`; see [docs/TENANCY.md](docs/TENANCY.md)). Encryption at rest is left to the deployment — Postgres/managed-PG/disk TDE stays transparent to queries, so search keeps working; memgres deliberately does **not** encrypt bodies application-side (that would make them unsearchable, which is why no comparable tool does it either). GDPR erasure is real: `forget()` hard-deletes the row, its vectors, and crypto-shreds the history chain. All limits are env-configurable, so the same code serves a single-user embed and a capped multi-tenant service.

---

## Quickstart

**Run everything with Docker** — `pgvector`, the memgres service (HTTP on `:8080`) and an MCP server (Streamable HTTP on `:8765`), schema auto-migrated on startup. Just grab the compose file (it pulls the published image, so there's nothing to build):

```bash
curl -O https://raw.githubusercontent.com/mozgsml/memgres/main/docker-compose.yml
docker compose up
```

Defaults suit a single-user setup with no auth. To change limits, the embedding provider, tokens, … drop a `.env` beside it — every `MEMGRES_*` is optional (see [Configuration](#configuration) or [.env.example](.env.example)).

**Give it to an LLM / agent — no code (MCP).** Point any URL-capable MCP client (Cursor, Cline, Claude Desktop, …) at the running server; the model gets `memory_write`, `memory_recall`, `memory_get`, `memory_list`, `memory_blame`, `memory_history`, `memory_move`, `memory_forget`, `memory_server_info` as tools:

```json
{
  "mcpServers": {
    "memgres": { "url": "http://localhost:8765/mcp" }
  }
}
```

Then just tell the model *"remember X"* / *"what do you know about Y?"* and it calls the tools — nothing else to run. (For stdio-only clients, semantic recall, and multi-tenant tokens, see [Use it with an LLM / agent (MCP)](#use-it-with-an-llm--agent-mcp) and [docs/TENANCY.md](docs/TENANCY.md).)

**Also a plain HTTP API** — the same service on `:8080` (`GET /healthz → {"ok":true}`), for when you drive the agent loop yourself:

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

```bash
pip install memgres              # core library
pip install "memgres[server]"    # + HTTP API
pip install "memgres[mcp]"       # + MCP server
# extras: local (sentence-transformers), qdrant (Qdrant backend)
```

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

Or pull the container image (public, no login):

```bash
docker pull ghcr.io/mozgsml/memgres:latest
```

### Three ways to run it

1. **`docker compose up`** — `pgvector` + service, nothing to configure. For a dedicated vector service instead, `docker compose --profile qdrant up` and set `MEMGRES_VECTOR_BACKEND=qdrant` (Qdrant ranks vectors; Postgres still holds bodies and does tag/subtree/TTL filtering).
2. **Your own Postgres** — install the `[server]` extra (above), point `MEMGRES_DATABASE_URL` at it, run `memgres-server` (migrates on startup).
3. **Embedded library** — install the core package, use `Store` directly, no HTTP at all.

Semantic recall is optional: the default `MEMGRES_EMBED_PROVIDER=none` gives you lexical FTS with zero models. Turn on `local` (sentence-transformers), a cloud API (`openai`/`jina`), or any OpenAI-compatible server (LM Studio, Ollama, …) when you want meaning-based search — see [docs/EMBEDDINGS.md](docs/EMBEDDINGS.md) for choosing local vs cloud and [docs/BACKENDS.md](docs/BACKENDS.md) for copy-paste setups. The model id + dimension get stamped into the schema and a later mismatch hard-fails instead of silently returning garbage.

## Configuration

Everything is env, all optional (defaults suit a single-user embed). Full list in [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `MEMGRES_DATABASE_URL` | libpq env | Postgres connection string |
| `MEMGRES_POOL_SIZE` | `4` | max pooled DB connections (HTTP + http-MCP servers); raise for many concurrent clients, `1` to serialize |
| `MEMGRES_MAX_BODY_BYTES` | `262144` | ceiling for a whole record body (256 KB) |
| `MEMGRES_MAX_WRITE_BYTES` | `16384` | ceiling for one write/diff payload (≤ body) |
| `MEMGRES_MAX_SOURCE_BYTES` / `_MAX_REASON_BYTES` | `2048` / `1024` | ceilings for a write's `source` / `reason` provenance |
| `MEMGRES_RETENTION_DAYS` | `0` | `0` = keep forever (TTL off); `>0` = expire N days after last touch |
| `MEMGRES_RENEW_ON_READ` | `true` | a read pushes the expiry clock forward |
| `MEMGRES_KEY_MODE` | `single` | `single` (no auth, one space) · `open` (bring-your-own token, self-registers) · `managed` (admin-provisioned). See [docs/TENANCY.md](docs/TENANCY.md) |
| `MEMGRES_ADMIN_TOKEN` | — | global admin bearer for provisioning (managed mode) |
| `MEMGRES_TOKEN` | — | default token used when a call passes none (single-tenant endpoints) |
| `MEMGRES_TREE` | `true` | `ltree` path column + GiST index (fast subtree select) |
| `MEMGRES_REQUIRE_PARENT` | `false` | `true` = a node's parent path must already exist |
| `MEMGRES_HISTORY` | `true` | keep the hash-chained diff history (deleted with the record) |
| `MEMGRES_FTS_LANGUAGE` | `simple` | Postgres FTS dictionary (`simple`/`english`/…) |
| `MEMGRES_LEXICAL_MATCH` | `any` | lexical query words OR-ed (`any`) or AND-ed (`all`); per-call `match` overrides |
| `MEMGRES_SNIPPET` | `true` | extract a best-match slice per hit (`MEMGRES_SNIPPET_*` tune size/semantic); `false` returns whole bodies |
| `MEMGRES_FULL_BODY` | `false` | force the whole body on every hit (off = auto: short whole, long sliced); `full_body` per call overrides |
| `MEMGRES_FULL_BODY_MAX_CHARS` | `500` | a body this short is returned whole (`kind="full"`) instead of sliced |
| `MEMGRES_LIST_PREVIEW_CHARS` | `120` | first-line preview length returned by `memory_list` |
| `MEMGRES_INSTRUCTION` | — | server-side MCP instructions emitted at `initialize` (a client like Claude Code loads it once at connect); unset = omitted; capped at 2 KB |
| `MEMGRES_VECTOR_BACKEND` | `pgvector` | `pgvector` (same DB) or `qdrant` (set `QDRANT_URL`, `QDRANT_API_KEY`, `MEMGRES_QDRANT_COLLECTION`) |
| `MEMGRES_EMBED_PROVIDER` | `none` | `none` / `local` / `openai` / `jina` / `openai-compatible` (LM Studio, Ollama, vLLM, TEI…) |
| `MEMGRES_EMBED_MODEL` / `_DIM` / `_API_KEY` / `_API_BASE` | — | model id · dimension (HTTP providers require it, `local` infers) · token · server URL |
| `MEMGRES_EMBED_MAX_SEQ` | `0` | override the local model's max input length in tokens (`0` = the model's default) |
| `MEMGRES_EMBED_ASYNC` | `false` | defer chunk-embedding to the background worker instead of embedding inline on the write. The server turns this on automatically when it runs a worker; leave it off for embedded/library use (writes embed synchronously, so semantic recall is correct immediately) |
| `MEMGRES_EMBED_WORKER` | `true` | the HTTP/MCP server runs a background embed worker |
| `MEMGRES_EMBED_WORKER_INTERVAL` / `_BATCH` | `1.0` / `16` | worker idle poll seconds · memories embedded per drain batch |

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
| `GET` | `/recall` | `?q=&k=&mode=&tags=&path_prefix=&match=&snippet=&full_body=` |
| `GET` | `/memories` | list a subtree, no query: `?path_prefix=&tags=&limit=&offset=` |
| `GET` | `/spaces` | namespaces this token can reach (identity modes) |
| `GET` | `/info` | effective config: limits, embed provider/model/dim, recall modes, backend |
| `GET` | `/healthz` | liveness |

Every memory/recall route also takes optional `space` (one of your namespaces by
name) and `space_id` (canonical id, for shared spaces). In `open`/`managed` mode
the token goes in `Authorization: Bearer <token>` or `X-Memgres-Token`; there are
also request-access and `/admin/*` provisioning routes — see
[docs/TENANCY.md](docs/TENANCY.md). OpenAPI/Swagger is at `/docs`. Store errors
map to status codes: `409` stale-hash conflict, `404` not found, `413` too large,
`401`/`403` auth.

`/healthz` and `/info` are intentionally unauthenticated (and the MCP
`memory_server_info` tool is always available): `/info` returns only effective
configuration — limits, embedding provider/model/dimension, available recall
modes, vector backend, key mode — and **never** a secret (no DB URL, token, API
key, or admin token). It exists so a client can discover a deployment's
capabilities before authenticating. If a `managed` deployment must not disclose
even that metadata, put it behind your reverse proxy's auth.

## Use it with an LLM / agent (MCP)

memgres itself **never calls an LLM** — it's the memory, not the model. Your LLM
uses it one of two ways:

**A. Via MCP** — the model calls memgres tools directly (Cursor, Cline, Claude
Desktop, any MCP client). Zero code.

`docker compose up` already starts an MCP server over Streamable HTTP at
**`http://localhost:8765/mcp`**. Point a URL-capable MCP client at it — nothing else
to run:

```json
{
  "mcpServers": {
    "memgres": { "url": "http://localhost:8765/mcp" }
  }
}
```

For **stdio-only** clients, install the command and let the client spawn it:

```bash
pip install "memgres[mcp]"
```
```json
{
  "mcpServers": {
    "memgres": {
      "command": "memgres-mcp",
      "env": {
        "MEMGRES_DATABASE_URL": "postgresql://memgres:memgres@localhost:5432/memgres",
        "MEMGRES_KEY_MODE": "open",
        "MEMGRES_TOKEN": "mgk_…"
      }
    }
  }
}
```

Either way the model gets tools `memory_write`, `memory_recall`, `memory_get`,
`memory_list`, `memory_blame`, `memory_history`, `memory_move`, `memory_forget`,
`memory_server_info`. Tell it *"remember X"* / *"what do you know about Y?"* and it
calls them. (For semantic recall add the embedding env vars — see
[docs/BACKENDS.md](docs/BACKENDS.md).)

### Isolation — pin the identity in the client config

The agent **never handles the token** (so the model spends nothing echoing a
secret and can't switch user):

- **stdio**: set `MEMGRES_KEY_MODE=open` + `MEMGRES_TOKEN=<mgk_…>` in the client's
  `env` block (above).
- **http**: send the token as a header — one shared endpoint then serves many
  clients, each pinned to its own user:
  ```json
  { "mcpServers": { "memgres": {
      "url": "http://localhost:8765/mcp",
      "headers": { "Authorization": "Bearer mgk_…" } } } }
  ```

A *namespace-scoped* token also locks the agent to one space. Only a genuinely
multi-tenant endpoint (open/managed, **no** pinned token) exposes a `token` tool
argument for the model to supply — force it either way with
`MEMGRES_MCP_TOKEN_ARG=on|off`. Single mode needs no token. Full model in
[docs/TENANCY.md](docs/TENANCY.md).

**B. From your own agent code** — your loop calls the HTTP API or the `Store`
library after the model produces text (see the examples above). Use this when you
control the agent loop and decide when to write/recall.

## Multi-tenant, tokens & isolation

There is **no token for single-user / local use** — leave everything default
(`MEMGRES_KEY_MODE=single`) and it just works. Two token concepts exist, unrelated:

- **Embedding API key** (`MEMGRES_EMBED_API_KEY`) — only if you use a *cloud*
  embedding provider (`openai`/`jina`) for semantic recall. Local models and
  lexical-only need none. This is the key from your embedding provider.
- **Access token** (multi-tenant, `MEMGRES_KEY_MODE=open|managed`) — a bearer
  credential of the form `mgk_` + 43 url-safe chars, authenticating *as a user*.
  Tokens are rotatable, expirable, revocable, and restrictable (a permission
  ceiling + optional scope to one namespace); the secret is stored only as a hash.
  Rotating a token does **not** move you to a new empty space — many tokens can
  back one user, and namespaces are addressed by name or id.

  ```bash
  python -c "import secrets; print('mgk_'+secrets.token_urlsafe(32))"   # open mode: mint your own
  ```

  Sent as `Authorization: Bearer <token>` / `X-Memgres-Token` (HTTP + MCP over
  http), the `token` argument (library), or `MEMGRES_TOKEN` in env for a
  single-tenant endpoint. Over MCP the agent never passes it — you pin it in the
  client config (env or headers). It's a bearer secret with **no recovery** —
  treat it like a password.

Full model — users, namespaces, permissions, request-access, admin
provisioning — in **[docs/TENANCY.md](docs/TENANCY.md)**.

---

## License

MIT — see [LICENSE](LICENSE). Fully self-hostable, no gated features.
