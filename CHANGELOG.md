# Changelog

All notable changes to memgres are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor = features/changes,
patch = fixes).

## [Unreleased]

### Added
- **Pluggable vector backend** (`memgres/vector/`): pgvector and Qdrant now sit
  behind one `VectorBackend` interface, so the store and search never branch on
  which one is configured and a new backend is a single module. Internal
  refactor — no behavior change.
- **`memory_list`** — browse/enumerate a subtree *without* a query (not a search:
  no full-text, no vectors), returning each memory's path, tags, a first-line
  preview, and timestamps. Available as the `memory_list` MCP tool and
  `GET /memories`. Preview length via `MEMGRES_LIST_PREVIEW_CHARS` (default 120).
- **`server_info`** — read-only introspection of the effective configuration
  (limits, embedding provider/model/dimension, available recall modes, vector
  backend, key mode). Carries no secrets. Available as the `memory_server_info`
  MCP tool and `GET /info`.
- **Configurable lexical match** — `MEMGRES_LEXICAL_MATCH` (`any` | `all`) plus a
  per-query `match` override on recall.
- **Provenance size caps** — `MEMGRES_MAX_SOURCE_BYTES` (default 2048) and
  `MEMGRES_MAX_REASON_BYTES` (default 1024); a write whose `source`/`reason`
  exceeds the cap is rejected, alongside the existing body/write ceilings.
- **`MEMGRES_EMBED_MAX_SEQ`** — override the local model's max sequence length, a
  guard against silently truncating long inputs when a model's default window is
  small.

### Changed
- **Lexical recall now defaults to OR (`any`)** — a query's words are OR-ed and
  ranked, so recall returns ranked partial matches instead of nothing when not
  every word is present. Set `MEMGRES_LEXICAL_MATCH=all` (or pass `match="all"`)
  for the previous AND-narrowing behavior.
- With no embedder (`MEMGRES_EMBED_PROVIDER=none`), the `memory_recall` MCP tool
  no longer advertises `semantic`/`hybrid` modes — the model only sees what works.
- pgvector writes the embedding via a separate `UPDATE` within the same write
  transaction (was inline in the INSERT). No visible behavior change.

### Documentation
- New `docs/EMBEDDINGS.md` — choosing and operating a local vs cloud embedding
  model, with the operational gotchas (dimension drift, context-window
  truncation, offline caches).

## [0.2.1]

### Added
- `MEMGRES_QDRANT_CA` — trust a self-signed / private-CA `https` Qdrant by
  pointing at its PEM certificate.
- Keyword payload index on `namespace` in the Qdrant backend, keeping
  tenant-filtered vector search fast as the collection grows.

## [0.2.0]

### Added
- **Identity & multi-tenancy** — users own namespaces; rotatable,
  permission-scoped `mgk_` tokens; `MEMGRES_KEY_MODE` = `single` | `open` |
  `managed`; admin provisioning and request-access over HTTP; the MCP identity is
  pinned in the client config so the model never handles tokens.
- Connection pool sized by `MEMGRES_POOL_SIZE` (HTTP + Streamable-HTTP MCP).

### Changed
- Removed the legacy `MEMGRES_NAMESPACES`; `MEMGRES_KEY_MODE` is the only tenancy
  mechanism.

### Security
- Closed a cross-tenant read and an MCP token-management privilege escalation
  (with adversarial isolation tests); constant-time admin-token comparison.

## [0.1.0]

Initial release: versioned document memory on one Postgres — whole-body or
unified-diff writes with content-hash optimistic concurrency, a hash-chained,
deletable history with git-like blame and reconstruct, an `ltree` tree plus tags,
and lexical (Postgres FTS) / semantic (pgvector or Qdrant) / hybrid (RRF) recall
behind pluggable embedding providers. HTTP (FastAPI) and MCP (stdio + Streamable
HTTP) layers. Published to PyPI and GHCR.
