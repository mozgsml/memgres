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
    # retention
    retention_days: int          # 0 = forever; >0 = expire N days after last touch
    renew_on_read: bool          # a read pushes the expiry clock forward
    # multi-tenant isolation
    token: str                   # default token used when a call passes none
                                 # (set in MCP/env for a single-tenant deployment)
    # identity / tenancy (see docs/TENANCY.md)
    key_mode: str                # single | open | managed  (how tokens/users are minted)
    admin_token: str             # global-admin bearer: provision users/namespaces anywhere
    # organization
    tree_enabled: bool           # ltree path column + GiST index for fast subtree selection
    require_parent: bool         # False = sparse paths (create food.apple with no food row);
                                 # True = a node's parent path must already exist as a memory
    # history
    history_enabled: bool        # keep hash-chained diff history (deleted with record)
    # search
    fts_language: str            # Postgres FTS dict: simple | english | russian | …
    vector_backend: str          # pgvector (default) | qdrant
    # listing / browse
    list_preview_chars: int      # first-line preview length for memory_list (0 = none)
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
        if self.embed_max_seq < 0:
            raise ValueError("MEMGRES_EMBED_MAX_SEQ must be >= 0")
        if self.list_preview_chars < 0:
            raise ValueError("MEMGRES_LIST_PREVIEW_CHARS must be >= 0")
        if self.max_write_bytes > self.max_body_bytes:
            raise ValueError(
                "MEMGRES_MAX_WRITE_BYTES must be <= MEMGRES_MAX_BODY_BYTES"
            )
        if self.embed_provider not in (
                "none", "local", "jina", "openai",
                "openai-compatible", "compatible", "custom"):
            raise ValueError(f"unknown MEMGRES_EMBED_PROVIDER: {self.embed_provider}")
        if self.vector_backend not in ("pgvector", "qdrant"):
            raise ValueError(f"unknown MEMGRES_VECTOR_BACKEND: {self.vector_backend}")
        if self.key_mode not in ("single", "open", "managed"):
            raise ValueError(f"unknown MEMGRES_KEY_MODE: {self.key_mode}")
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
        retention_days=_int("MEMGRES_RETENTION_DAYS", 0),
        renew_on_read=_bool("MEMGRES_RENEW_ON_READ", True),
        token=_str("MEMGRES_TOKEN", ""),
        key_mode=_str("MEMGRES_KEY_MODE", "single"),
        admin_token=_str("MEMGRES_ADMIN_TOKEN", ""),
        tree_enabled=_bool("MEMGRES_TREE", True),
        require_parent=_bool("MEMGRES_REQUIRE_PARENT", False),
        history_enabled=_bool("MEMGRES_HISTORY", True),
        fts_language=_str("MEMGRES_FTS_LANGUAGE", "simple"),
        vector_backend=_str("MEMGRES_VECTOR_BACKEND", "pgvector"),
        list_preview_chars=_int("MEMGRES_LIST_PREVIEW_CHARS", 120),
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
