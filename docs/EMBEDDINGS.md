# Embedding models — local or cloud

Semantic recall needs an embedding model to turn text into vectors. memgres treats
this as one swappable axis: pick a provider with `MEMGRES_EMBED_PROVIDER` and the
same store, search, and schema work unchanged. This guide is about **choosing and
operating that model** — local vs cloud, the trade-offs, and the few things that
bite if you get them wrong. For the terse copy-paste table and the *vector backend*
(where vectors are stored), see [BACKENDS.md](BACKENDS.md).

You don't need a model at all: the default `MEMGRES_EMBED_PROVIDER=none` gives you
lexical full-text search with zero dependencies. Add a model only when you want
meaning-based (`semantic`/`hybrid`) recall.

---

## Local or cloud — how to choose

| | **Local** (`local`, or a self-hosted OpenAI-compatible server) | **Cloud** (`openai`, `jina`, or a hosted compatible API) |
|---|---|---|
| Text leaves your machine | **No** — everything stays in-process / on your network | **Yes** — every write and query is sent to the provider |
| Dependencies | `sentence-transformers` + torch (the `[local]` extra, ~GB) | none beyond stdlib (HTTP via urllib) |
| Runs on | your CPU/GPU | the provider's infrastructure |
| Offline / air-gapped | works | needs network |
| Cold start | model load (seconds) + first-run download | first HTTP call |
| Quality ceiling | strong open models exist, but you run them | top hosted models, no ops |

Rules of thumb:

- **Private / offline / self-hosted** → `local` (or a self-hosted server via
  `openai-compatible`). The text never leaves your infrastructure.
- **No heavy dependencies, don't want to run a model** → `openai` / `jina`. Accept
  that bodies and queries are sent to the provider.
- **You already run an inference server** (LM Studio, Ollama, vLLM, TEI, LocalAI) →
  `openai-compatible` pointed at it: local privacy without pulling torch into the
  memgres process.

---

## Providers and settings

Shared env for any model:

- `MEMGRES_EMBED_MODEL` — model id.
- `MEMGRES_EMBED_DIM` — vector dimension. **Required for every HTTP provider**
  (`openai`/`jina`/`openai-compatible`); `local` infers it from the loaded model.
- `MEMGRES_EMBED_API_KEY` — for hosted APIs (optional for a keyless local server).
- `MEMGRES_EMBED_API_BASE` — server URL (required for `openai-compatible`; overrides
  the default endpoint for `openai`/`jina`).

### `local` — sentence-transformers in-process

```bash
pip install "memgres[local]"           # pulls sentence-transformers + torch

MEMGRES_EMBED_PROVIDER=local
MEMGRES_EMBED_MODEL=BAAI/bge-m3        # any sentence-transformers model id
# MEMGRES_EMBED_DIM is inferred from the model
```

Runs on CPU by default. torch from PyPI may bundle CUDA; for a CPU-only box install
the CPU wheel first to save ~2 GB:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install "memgres[local]"
```

**Reuse an already-downloaded model / go offline.** sentence-transformers caches
models under the HuggingFace hub cache. Point `HF_HOME` at an existing cache to avoid
re-downloading, and set `HF_HUB_OFFLINE=1` to forbid any network fetch (fails loudly
if the model isn't cached, instead of hanging):

```bash
HF_HOME=/path/to/hf-cache
HF_HUB_OFFLINE=1
```

### `openai` — OpenAI cloud

```bash
MEMGRES_EMBED_PROVIDER=openai
MEMGRES_EMBED_MODEL=text-embedding-3-small
MEMGRES_EMBED_DIM=1536
MEMGRES_EMBED_API_KEY=sk-...
```

### `jina` — Jina cloud

Sends passage/query task hints (documents and search queries are embedded with
different task types, which helps retrieval).

```bash
MEMGRES_EMBED_PROVIDER=jina
MEMGRES_EMBED_MODEL=jina-embeddings-v3
MEMGRES_EMBED_DIM=1024
MEMGRES_EMBED_API_KEY=jina_...
```

### `openai-compatible` — any OpenAI-shaped `/embeddings` server

LM Studio, Ollama, vLLM, TEI, LocalAI, or any hosted API that speaks the OpenAI
embeddings shape. Base URL required; key optional.

```bash
MEMGRES_EMBED_PROVIDER=openai-compatible
MEMGRES_EMBED_API_BASE=http://localhost:11434/v1   # e.g. Ollama
MEMGRES_EMBED_MODEL=nomic-embed-text
MEMGRES_EMBED_DIM=768
```

---

## Operating an embedding model — the parts that bite

### 1. The model + dimension are stamped; a mismatch hard-fails

On first migrate memgres records the model id and dimension in the schema. If a later
run computes a different dimension (you switched models), it **stops with an error**
rather than silently mixing incompatible vectors. This is deliberate: the alternative
is a search that quietly returns nonsense.

**Switching models (or dimensions) after data exists = re-embed.** Existing vectors
were computed by the old model and don't live in the new space. Point at a fresh
vector store (a new pgvector column via a clean schema, or a fresh
`MEMGRES_QDRANT_COLLECTION`) and re-embed the bodies. Don't mix models in one store.

### 2. Context window — chunking covers the whole body

Every embedding model has a maximum input length; text beyond it is truncated
**before** it's encoded. memgres defends against this by indexing a memory as
overlapping **chunks** (sized by `MEMGRES_SNIPPET_SEG_CHARS`, default 400 chars)
rather than one vector of the whole body: each chunk is embedded and ranked
independently, and recall keeps the best-matching chunk per memory. So the tail of
a long memory is searchable, and a match deep in a document isn't drowned out by a
single averaged vector — the failure mode ("only the beginning is embedded, the
rest is invisible, with no error") no longer applies to whole memories.

- A single **chunk** must still fit the model's window. Keep `SNIPPET_SEG_CHARS`
  comfortably under the window (in characters ≈ tokens × 4). The default (400
  chars) is safe for any real model.
- With `local`, sentence-transformers uses the model's configured `max_seq_length`;
  `MEMGRES_EMBED_MAX_SEQ` overrides it if the default is a small surprise.

Whenever you change the indexing path, **verify actual recall quality on real
queries** — not just "it ran without error." Silent degradation doesn't raise.

### 2b. Embedding runs off the write path (server)

On a server, a write flags the memory and returns immediately; a background worker
segments, embeds, and indexes it (`MEMGRES_EMBED_WORKER`, on by default). So a
write is fast no matter how large the body or how slow the model — semantic recall
for that memory becomes available a moment later, once the worker drains it
(`MEMGRES_EMBED_WORKER_INTERVAL`). Lexical recall is immediate either way.

Embedded/library use (a `Store` with no server loop) keeps embedding **inline** by
default (`MEMGRES_EMBED_ASYNC=false`), so a write is semantically searchable the
instant it commits — no worker to run. Turn `MEMGRES_EMBED_ASYNC=true` on only if
you drive draining yourself.

### 3. Documents vs queries

Some models (and `jina`) embed a passage and a search query differently. memgres
already calls the right side for you — writes use the document embedding, `recall`
uses the query embedding — so you don't manage this, but it's why a model's own
"query prefix / passage prefix" guidance is handled internally: don't pre-apply it to
your text.

### 4. Lexical still works without any of this

`MEMGRES_EMBED_PROVIDER=none` (or `mode=lexical` on a recall) needs no model and does
exact, language-aware full-text matching over the **whole** body. Semantic search
sees only what fits the model window; lexical sees everything. `hybrid` fuses both —
useful when exact identifiers matter as much as meaning. See the recall modes in the
main [README](../README.md).

---

## Quick recipes

**Fully local & offline, semantic in Postgres:**
```bash
MEMGRES_EMBED_PROVIDER=local
MEMGRES_EMBED_MODEL=BAAI/bge-m3
HF_HOME=/path/to/hf-cache
HF_HUB_OFFLINE=1
MEMGRES_VECTOR_BACKEND=pgvector
```

**Local privacy without torch in-process (separate inference server):**
```bash
MEMGRES_EMBED_PROVIDER=openai-compatible
MEMGRES_EMBED_API_BASE=http://localhost:1234/v1
MEMGRES_EMBED_MODEL=<model loaded in your server>
MEMGRES_EMBED_DIM=768
```

**Hosted, no local deps:**
```bash
MEMGRES_EMBED_PROVIDER=openai
MEMGRES_EMBED_MODEL=text-embedding-3-small
MEMGRES_EMBED_DIM=1536
MEMGRES_EMBED_API_KEY=sk-...
```

See [BACKENDS.md](BACKENDS.md) for where the vectors are **stored** (pgvector vs
Qdrant) and [`.env.example`](../.env.example) for the full settings list.
