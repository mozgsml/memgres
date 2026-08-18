# Changelog

All notable changes to memgres are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor = features/changes,
patch = fixes).

## [Unreleased]

### Added
- **Curated `title` + `memory_find`** — a memory can carry a short, human-curated
  `title` (set whole, distinct from the body's first-line preview), returned on
  `get`/`list`/recall hits. `memory_find` (MCP) / `GET /find` locate memories by
  their **title + tags** only — light rows `{id, path, title, tags, score}`, never
  the body, no vectors — a cheap "where is it?" scan before a heavier recall (works
  without an embedder). Title changes are audited in the hash-chained history
  (`title_before`/`title_after`, op `retitle`) and folded into the chain **only
  when the title actually changes**, the same domain-separated way as author — so
  every pre-title row keeps its exact digest and still verifies. New
  `MEMGRES_MAX_TITLE_BYTES` (default 256), reported in `server_info`.
- **Substring edit (`replace`)** — edit a memory by sending `replace_old` →
  `replace_new` instead of hand-building a unified diff: the server finds
  `replace_old` in the current body and rewrites just it. `replace_old` must be
  unique unless `replace_all=true` (else a clear error asks for more context);
  a missing `replace_old` or a no-op (old == new) is rejected, never a silent
  write. Because only `old`+`new` cross the wire (size-capped), a body larger
  than `MEMGRES_MAX_WRITE_BYTES` stays editable — which a whole-body rewrite
  can't do. It lowers to the existing diff+OCC path, so history stays a single
  replayable, line-attributable chain (`base_hash` optional here; supplied adds
  strict OCC). On the `memory_write` MCP tool and `PATCH /memories/{id}`.
- **`server_info` now reports `version` and `schema_version`** — a client can tell
  which memgres it's talking to (and which DB layout) without guessing. The
  version is read from code (`memgres.__version__`), so an editable/dev checkout
  reports what it's actually running, not stale install metadata. Exposed on both
  the `memory_server_info` MCP tool and `GET /info`.
- **Authoritative authorship in history** — every `memory_history` row now records
  the server-resolved principal (`author_user_id` + `author_token_id`) on each
  write, separate from the free-text `source`/`reason` a client supplies. In a
  shared namespace this answers *who* actually made an edit, not just who claims
  to have. `history`, `blame` (per-line + grouped), the `memory_history` /
  `memory_blame` MCP tools and the HTTP `…/history` / `…/blame` endpoints expose
  it, resolving `author_name` from the user row via LEFT JOIN (a since-deleted
  author reads back as its bare id). The author is folded into the tamper-evident
  hash chain, so stripping or swapping authorship is detectable by
  `verify_history`.

### Notes
- Backward compatible: user-less writes (single mode, and the global-admin env
  token) stamp NULL author and hash **exactly** as before, so history chains
  written before this release still verify. New columns are added by an
  idempotent migration (schema v4); no reindex or downtime.
- No foreign key ties the author columns to `app_user`/`token`: the history is an
  immutable audit record, so deleting a user must not mutate (and break the
  verifiability of) unrelated memories' chains. A dedicated author-purge is a
  future admin op.

## [0.3.2] — 2026-07-31

### Fixed
- **Silent no-op on a malformed diff.** `apply_diff` skipped any line that wasn't
  a valid `@@ -a,b +c,d @@` header, so a patch with a malformed hunk header (or
  none at all) applied nothing and returned the body unchanged — while the write
  still bumped `seq`/`updated_at`, looking like success. It now raises
  `DiffConflict` (HTTP 409) instead, and a `diff` write that leaves the body
  identical is likewise rejected. Empty patches remain a legitimate no-op.

## [0.3.1] — 2026-07-31

### Fixed
- Compatibility with the **mcp SDK 2.0**, which removed the `mcp.server.fastmcp`
  module (renamed to `mcp.server.mcpserver`). The unguarded `Context` import
  broke `memgres.mcp_server` on mcp ≥ 2.0 — now imported from either path.
  memgres works on mcp 1.x and 2.x. (0.3.0 shipped with this import bug.)

## [0.3.0] — 2026-07-31

### Added
- **Recall snippets** — each hit now carries a `snippet` (the most relevant slice
  of its body) plus a `line` number. Semantic/hybrid hits pick their
  best-matching *segment*, lazily embedded and cached per body-hash (recomputed
  when the body changes); lexical/hybrid fall back to Postgres `ts_headline`.
  Tunable via `MEMGRES_SNIPPET*` settings plus per-call `snippet` / `full_body`
  params on `memory_recall` and `GET /recall` — pass `full_body=false` for just
  the snippet.
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

### Security
- Segment-cache reads (`get_segments`) now filter by `namespace` in addition to
  `memory_id`, so a tenant's snippet cache is *structurally* scoped rather than
  relying on memory-id unguessability. Defense-in-depth — not a fixed exploit
  (the id was already sourced from a namespace-scoped recall). Verified by an
  adversarial cross-tenant test (`test_qdrant_two_namespaces_isolated`).
- `GET /info` / `memory_server_info` are unauthenticated by design and return no
  secrets (config metadata only) — documented so `managed` deployments can gate
  it at the proxy if even that must stay private.

### Notes
- With a **paid** embedding API, semantic snippets add model calls on first sight
  of each hit (segments are embedded, then cached). Set
  `MEMGRES_SNIPPET_SEMANTIC=false` to disable them and use `ts_headline` instead.
  With a local model the cost is negligible (CPU/GPU only).

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
