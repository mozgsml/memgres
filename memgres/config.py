"""All memgres settings, read from the environment.

Every operational limit lives here so the same code serves two very different
deployments from env alone:

  * single-user / embedded — no namespaces, unlimited retention, write whole
    bodies directly;
  * multi-tenant / metered — token namespaces required, retention
    capped, single writes capped small so large memories accrue over many
    (paid) diffs.

Nothing here is specific to a language or a project. The default embedding
provider is ``none`` (lexical search still works); turn on a local or cloud
model when you want semantic recall.

Env is read in :func:`load`, not at import time, so a process that sets env
after importing this module still gets the right values, and tests can vary it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw not in (None, "") else default


@dataclass(frozen=True)
class Config:
    # storage limits (bytes)
    max_body_bytes: int          # whole-record ceiling; grows to this via diffs
    max_write_bytes: int         # one write/diff payload ceiling (<= max_body)
    max_source_bytes: int        # provenance `source` field ceiling (per write)
    max_reason_bytes: int        # provenance `reason` field ceiling (per write)
    max_title_bytes: int         # curated `title` field ceiling (per write)
    # retention
    retention_days: int          # 0 = forever; >0 = expire N days after last touch
    renew_on_read: bool          # a read pushes the expiry clock forward
    # multi-tenant isolation
    default_token: str           # default token used when a call passes none
                                 # (set in MCP/env for a single-tenant deployment)
    # identity / tenancy (see docs/TENANCY.md)
    key_mode: str                # single | open | managed  (how tokens/users are minted)
    admin_token: str             # bootstrap/break-glass bearer (managed): seeds the
                                 # first service admin at startup, then resolves to
                                 # that real user (see memgres.bootstrap)
    admin_token_file: str        # read-or-create path for the bootstrap token
                                 # (Jenkins-style): present → read it; missing/empty
                                 # → generate an mgk_ token, write it 0600, log the
                                 # path only. Mutually exclusive with admin_token.
    admin_role: str              # role the bootstrap admin is seeded with:
                                 # user_manager (default) | superadmin
    token_sink: str              # directory a freshly minted token secret is
                                 # WRITTEN to (0600) instead of being returned in
                                 # the reply. Set it when the caller is an agent:
                                 # a secret in a tool result is a secret in a chat
                                 # transcript, and every transcript is copied,
                                 # summarized and stored. Empty = return it.
    # organization
    tree_enabled: bool           # ltree path column + GiST index for fast subtree selection
    require_title: bool          # True = a write that stores CONTENT must caption it
    require_parent: bool         # False = sparse paths (create food.apple with no food row);
                                 # True = a node's parent path must already exist as a memory
    # history
    history_enabled: bool        # keep hash-chained diff history (deleted with record)
    # search
    fts_language: str            # Postgres FTS dict: simple | english | russian | …
    lexical_match: str           # any (OR-any words, default) | all (AND-all words)
    vector_backend: str          # pgvector (default) | qdrant
    # snippets (a relevant slice of each recall hit's body + its line range)
    snippet: bool                # extract a relevant slice; off = return the body
    full_body: bool              # force the whole body on every hit (off = auto:
                                 # short bodies whole, long bodies sliced)
    full_body_max_chars: int     # a body this short is returned whole (kind=full)
                                 # instead of sliced — a slice would just repeat it
    snippet_semantic: bool       # semantic/hybrid hits use the best-matching
                                 # segment (needs the model); off = ts_headline,
                                 # avoiding per-query model calls on a paid API
    chunk_chars: int             # chunk size for the chunk index (ranking+snippet);
                                 # MEMGRES_CHUNK_CHARS (legacy MEMGRES_SNIPPET_SEG_CHARS)
    chunk_overlap: int           # chars shared between consecutive chunks
                                 # (MEMGRES_CHUNK_OVERLAP / legacy _SNIPPET_SEG_OVERLAP)
    # embedding pipeline (chunks are the semantic index; see docs/EMBEDDINGS.md)
    embed_dispatch: str          # how a write's chunk-embedding happens:
                                 #   inline — embed within the write (safe default;
                                 #     library/embedded use, no worker needed);
                                 #   async  — flag embed_pending and return; a worker
                                 #     (in-process or a separate memgres-worker)
                                 #     drains it. A SERVER upgrades inline→async when
                                 #     it starts an in-process worker (see
                                 #     embed_worker.wire_server); set it to async
                                 #     explicitly WITH embed_worker=off for a split
                                 #     deployment where an external worker embeds.
    embed_worker: bool           # a server process runs an in-process embed worker
    embed_worker_interval: float # seconds the idle worker sleeps between drains
    usage_counters: bool         # count how often each memory surfaces in search and
                                 # is read in full. Off makes reads pure again — for
                                 # a read-only replica, or a deployment unwilling to
                                 # pay one small write per read.
    retention_sweep: bool        # this process runs the retention sweep
    retention_sweep_interval: float  # seconds between retention sweeps (see retention_days)
    embed_max_attempts: int      # after this many failed embed attempts a row is a
                                 # dead letter — left flagged but out of the claim
                                 # rotation (logged), so one poison body can't wedge
                                 # the queue behind it. A successful embed resets it.
    embed_retry_backoff_s: float # seconds a failed row is skipped before retry
    # listing / browse
    list_preview_chars: int      # first-line preview length for memory_list (0 = none)
    list_bodies_max_bytes: int   # total body bytes one `bodies=true` browse may return
    # embeddings
    embed_provider: str          # none | local | jina | openai | openai-compatible
    embed_model: str
    embed_dim: int               # 0 = infer from provider
    embed_api_key: str
    embed_api_base: str
    embed_max_seq: int           # 0 = leave the model's default; >0 overrides
                                 # the local model's max sequence length (tokens)
    # database
    database_url: str
    pool_size: int               # max pooled connections (HTTP + http-MCP servers)

    def validate(self) -> None:
        if self.pool_size < 1:
            raise ValueError("MEMGRES_POOL_SIZE must be >= 1")
        if self.max_source_bytes < 1:
            raise ValueError("MEMGRES_MAX_SOURCE_BYTES must be >= 1")
        if self.max_reason_bytes < 1:
            raise ValueError("MEMGRES_MAX_REASON_BYTES must be >= 1")
        if self.max_title_bytes < 1:
            raise ValueError("MEMGRES_MAX_TITLE_BYTES must be >= 1")
        if self.embed_max_seq < 0:
            raise ValueError("MEMGRES_EMBED_MAX_SEQ must be >= 0")
        if self.list_preview_chars < 0:
            raise ValueError("MEMGRES_LIST_PREVIEW_CHARS must be >= 0")
        if self.list_bodies_max_bytes < 1:
            raise ValueError("MEMGRES_LIST_BODIES_MAX_BYTES must be >= 1")
        if self.chunk_chars < 1:
            raise ValueError("MEMGRES_CHUNK_CHARS must be >= 1")
        if self.chunk_overlap < 0:
            raise ValueError("MEMGRES_CHUNK_OVERLAP must be >= 0")
        if self.embed_dispatch not in ("inline", "async"):
            raise ValueError(f"unknown MEMGRES_EMBED_DISPATCH: {self.embed_dispatch}")
        if self.full_body_max_chars < 0:
            raise ValueError("MEMGRES_FULL_BODY_MAX_CHARS must be >= 0")
        if self.embed_worker_interval <= 0:
            raise ValueError("MEMGRES_EMBED_WORKER_INTERVAL must be > 0")
        if self.retention_sweep_interval <= 0:
            raise ValueError("MEMGRES_RETENTION_SWEEP_INTERVAL must be > 0")
        if self.embed_max_attempts < 1:
            raise ValueError("MEMGRES_EMBED_MAX_ATTEMPTS must be >= 1")
        if self.embed_retry_backoff_s < 0:
            raise ValueError("MEMGRES_EMBED_RETRY_BACKOFF_S must be >= 0")
        if self.max_write_bytes > self.max_body_bytes:
            raise ValueError(
                "MEMGRES_MAX_WRITE_BYTES must be <= MEMGRES_MAX_BODY_BYTES"
            )
        if self.embed_provider not in (
                "none", "local", "jina", "openai",
                "openai-compatible", "compatible", "custom"):
            raise ValueError(f"unknown MEMGRES_EMBED_PROVIDER: {self.embed_provider}")
        if self.lexical_match not in ("any", "all"):
            raise ValueError(f"unknown MEMGRES_LEXICAL_MATCH: {self.lexical_match}")
        if self.vector_backend not in ("pgvector", "qdrant"):
            raise ValueError(f"unknown MEMGRES_VECTOR_BACKEND: {self.vector_backend}")
        if self.key_mode not in ("single", "open", "managed"):
            raise ValueError(f"unknown MEMGRES_KEY_MODE: {self.key_mode}")
        if self.admin_role not in ("user_manager", "superadmin"):
            raise ValueError(
                "MEMGRES_ADMIN_ROLE must be user_manager or superadmin "
                f"(got {self.admin_role!r})")
        if self.token_sink and not os.path.isabs(self.token_sink):
            # A relative sink resolves against each process's CWD, so the server
            # and the CLI would write the same operator's secrets to different
            # directories — and neither would say so.
            raise ValueError("MEMGRES_TOKEN_SINK must be an absolute path")
        if self.admin_token and self.admin_token_file:
            raise ValueError(
                "set only one of MEMGRES_ADMIN_TOKEN / MEMGRES_ADMIN_TOKEN_FILE")
        if self.embed_provider != "none" and self.vector_backend == "pgvector" \
                and self.embed_dim <= 0:
            raise ValueError(
                "semantic search needs a vector dimension: set MEMGRES_EMBED_DIM "
                "(or it is inferred once the provider loads)"
            )


def load() -> Config:
    """Build Config from the current environment and validate it."""
    cfg = Config(
        max_body_bytes=_int("MEMGRES_MAX_BODY_BYTES", 262_144),      # 256 KB
        max_write_bytes=_int("MEMGRES_MAX_WRITE_BYTES", 16_384),      # 16 KB
        max_source_bytes=_int("MEMGRES_MAX_SOURCE_BYTES", 2_048),     # 2 KB
        max_reason_bytes=_int("MEMGRES_MAX_REASON_BYTES", 1_024),     # 1 KB
        max_title_bytes=_int("MEMGRES_MAX_TITLE_BYTES", 256),         # 256 B
        retention_days=_int("MEMGRES_RETENTION_DAYS", 0),
        renew_on_read=_bool("MEMGRES_RENEW_ON_READ", True),
        default_token=_str("MEMGRES_TOKEN", ""),
        key_mode=_str("MEMGRES_KEY_MODE", "single"),
        admin_token=_str("MEMGRES_ADMIN_TOKEN", ""),
        admin_token_file=_str("MEMGRES_ADMIN_TOKEN_FILE", ""),
        admin_role=_str("MEMGRES_ADMIN_ROLE", "user_manager"),
        token_sink=_str("MEMGRES_TOKEN_SINK", ""),
        tree_enabled=_bool("MEMGRES_TREE", True),
        require_title=_bool("MEMGRES_REQUIRE_TITLE", True),
        require_parent=_bool("MEMGRES_REQUIRE_PARENT", False),
        history_enabled=_bool("MEMGRES_HISTORY", True),
        fts_language=_str("MEMGRES_FTS_LANGUAGE", "simple"),
        lexical_match=_str("MEMGRES_LEXICAL_MATCH", "any"),
        vector_backend=_str("MEMGRES_VECTOR_BACKEND", "pgvector"),
        snippet=_bool("MEMGRES_SNIPPET", True),
        full_body=_bool("MEMGRES_FULL_BODY", False),
        full_body_max_chars=_int("MEMGRES_FULL_BODY_MAX_CHARS", 500),
        snippet_semantic=_bool("MEMGRES_SNIPPET_SEMANTIC", True),
        chunk_chars=_int("MEMGRES_CHUNK_CHARS",
                         _int("MEMGRES_SNIPPET_SEG_CHARS", 400)),
        chunk_overlap=_int("MEMGRES_CHUNK_OVERLAP",
                           _int("MEMGRES_SNIPPET_SEG_OVERLAP", 80)),
        embed_dispatch=_str("MEMGRES_EMBED_DISPATCH", "inline"),
        embed_worker=_bool("MEMGRES_EMBED_WORKER", True),
        embed_worker_interval=_float("MEMGRES_EMBED_WORKER_INTERVAL", 1.0),
        usage_counters=_bool("MEMGRES_USAGE_COUNTERS", True),
        retention_sweep=_bool("MEMGRES_RETENTION_SWEEP", True),
        retention_sweep_interval=_float("MEMGRES_RETENTION_SWEEP_INTERVAL", 3600.0),
        embed_max_attempts=_int("MEMGRES_EMBED_MAX_ATTEMPTS", 5),
        embed_retry_backoff_s=_float("MEMGRES_EMBED_RETRY_BACKOFF_S", 60.0),
        list_preview_chars=_int("MEMGRES_LIST_PREVIEW_CHARS", 120),
        list_bodies_max_bytes=_int("MEMGRES_LIST_BODIES_MAX_BYTES", 200_000),
        embed_provider=_str("MEMGRES_EMBED_PROVIDER", "none"),
        embed_model=_str("MEMGRES_EMBED_MODEL", ""),
        embed_dim=_int("MEMGRES_EMBED_DIM", 0),
        embed_api_key=_str("MEMGRES_EMBED_API_KEY", ""),
        embed_api_base=_str("MEMGRES_EMBED_API_BASE", ""),
        embed_max_seq=_int("MEMGRES_EMBED_MAX_SEQ", 0),
        database_url=_str("MEMGRES_DATABASE_URL", ""),
        pool_size=_int("MEMGRES_POOL_SIZE", 4),
    )
    cfg.validate()
    return cfg
