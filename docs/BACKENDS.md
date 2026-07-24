# Backends & configuration

memgres has two independent, env-driven axes. Change either without touching code
or data model — only re-embedding requires care (the schema stamps the model, so a
mismatch hard-fails instead of silently returning garbage).

1. **Embedding provider** — *how vectors are computed* (or not, for lexical-only).
2. **Vector backend** — *where vectors are stored & ranked* (Postgres or Qdrant).

---

## 1. Embedding providers (`MEMGRES_EMBED_PROVIDER`)

| Provider | Vectors from | Needs | Notes |
|---|---|---|---|
| `none` (default) | — | nothing | Lexical FTS only; no model, API, or GPU. |
| `local` | sentence-transformers in-process | `[local]` extra | Runs on CPU/GPU; dimension inferred from the model. |
| `openai` | OpenAI cloud | API key | `text-embedding-3-small` etc. |
| `jina` | Jina cloud | API key | Sends passage/query task hints. |
| `openai-compatible` | any OpenAI-shaped `/embeddings` server | base URL | LM Studio, Ollama, vLLM, TEI, LocalAI… key optional. |

Shared settings: `MEMGRES_EMBED_MODEL` (model id), `MEMGRES_EMBED_DIM` (required for
every HTTP provider; `local` infers it), `MEMGRES_EMBED_API_KEY`,
`MEMGRES_EMBED_API_BASE` (server URL).

**Switching local ↔ cloud ↔ self-hosted is one variable — `MEMGRES_EMBED_PROVIDER`.**
Changing the *model or dimension* after data exists means the stored vectors no
longer match; re-embed into a fresh collection (the schema guard will stop you from
mixing them by accident).

### Examples

**Lexical only — no embeddings (default):**
```bash
MEMGRES_EMBED_PROVIDER=none
# recall works via Postgres full-text search; mode=semantic/hybrid unavailable
```

**Local (sentence-transformers)** — `pip install "…[local]"`:
```bash
MEMGRES_EMBED_PROVIDER=local
MEMGRES_EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
# MEMGRES_EMBED_DIM is inferred from the model
```

**OpenAI cloud:**
```bash
MEMGRES_EMBED_PROVIDER=openai
MEMGRES_EMBED_MODEL=text-embedding-3-small
MEMGRES_EMBED_DIM=1536
MEMGRES_EMBED_API_KEY=sk-...
```

**LM Studio** (OpenAI-compatible, local server, no key):
```bash
MEMGRES_EMBED_PROVIDER=openai-compatible
MEMGRES_EMBED_API_BASE=http://localhost:1234/v1
MEMGRES_EMBED_MODEL=<the model you loaded in LM Studio>
MEMGRES_EMBED_DIM=768
```

**Ollama** (OpenAI-compatible endpoint):
```bash
MEMGRES_EMBED_PROVIDER=openai-compatible
MEMGRES_EMBED_API_BASE=http://localhost:11434/v1
MEMGRES_EMBED_MODEL=nomic-embed-text
MEMGRES_EMBED_DIM=768
```

**Jina cloud:**
```bash
MEMGRES_EMBED_PROVIDER=jina
MEMGRES_EMBED_MODEL=jina-embeddings-v3
MEMGRES_EMBED_DIM=1024
MEMGRES_EMBED_API_KEY=jina_...
```

---

## 2. Vector backend (`MEMGRES_VECTOR_BACKEND`)

Only relevant once an embedding provider is set (lexical-only needs no vector store).

| Backend | Where vectors live | When to pick |
|---|---|---|
| `pgvector` (default) | in the `memory` table, same Postgres | one datastore, one backup; simplest. |
| `qdrant` | a separate Qdrant service | you already run Qdrant, or want a dedicated ANN service. |

**How Qdrant mode splits the work:** Qdrant stores only the vector + the memory's
`namespace` and ranks by cosine similarity; **Postgres stays the source of truth for
bodies and for every other filter** (tags, subtree, TTL). Semantic recall ranks in
Qdrant, then fetches and filters the candidates in Postgres. So tag/tree/TTL edits
never touch Qdrant — only a body change re-embeds (upsert) and `forget` deletes the
point.

### pgvector (default)
```bash
MEMGRES_VECTOR_BACKEND=pgvector      # nothing else to run
```

### Qdrant
```bash
MEMGRES_VECTOR_BACKEND=qdrant
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=                      # if your Qdrant requires one
MEMGRES_QDRANT_COLLECTION=memgres    # default
```
With docker compose, start Qdrant alongside the service:
```bash
docker compose --profile qdrant up
```
(The compose file points the service at `http://qdrant:6333` automatically.)

---

## Putting it together — full example configs

**Single-user, semantic, everything in one Postgres (local model):**
```bash
MEMGRES_DATABASE_URL=postgresql://memgres:memgres@localhost:5432/memgres
MEMGRES_EMBED_PROVIDER=local
MEMGRES_EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
MEMGRES_VECTOR_BACKEND=pgvector
```

**Multi-tenant service, cloud embeddings, dedicated Qdrant:**
```bash
MEMGRES_DATABASE_URL=postgresql://…
MEMGRES_KEY_MODE=managed
MEMGRES_EMBED_PROVIDER=openai
MEMGRES_EMBED_MODEL=text-embedding-3-small
MEMGRES_EMBED_DIM=1536
MEMGRES_EMBED_API_KEY=sk-...
MEMGRES_VECTOR_BACKEND=qdrant
QDRANT_URL=https://your-qdrant:6333
QDRANT_API_KEY=...
```

**Air-gapped / offline, self-hosted model, lexical + semantic in Postgres:**
```bash
MEMGRES_EMBED_PROVIDER=openai-compatible
MEMGRES_EMBED_API_BASE=http://lmstudio:1234/v1
MEMGRES_EMBED_MODEL=<local model>
MEMGRES_EMBED_DIM=768
MEMGRES_VECTOR_BACKEND=pgvector
```

See [`.env.example`](../.env.example) for the complete list of settings.
