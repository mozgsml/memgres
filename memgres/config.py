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
    # retention
    retention_days: int          # 0 = forever; >0 = expire N days after last touch
    renew_on_read: bool          # a read pushes the expiry clock forward
    # multi-tenant isolation
    namespaces_enabled: bool     # False = single space; True = secret-token namespaces
    token: str                   # default namespace token when a call passes none
                                 # (set in MCP/env for a single-tenant deployment)
    # organization
    tree_enabled: bool           # ltree path column + GiST index for fast subtree selection
    require_parent: bool         # False = sparse paths (create food.apple with no food row);
                                 # True = a node's parent path must already exist as a memory
    # history
    history_enabled: bool        # keep hash-chained diff history (deleted with record)
    # search
    fts_language: str            # Postgres FTS dict: simple | english | russian | …
    vector_backend: str          # pgvector (default) | qdrant
    # embeddings
    embed_provider: str          # none | local | jina | openai | openai-compatible
    embed_model: str
    embed_dim: int               # 0 = infer from provider
    embed_api_key: str
    embed_api_base: str
    # database
    database_url: str

    def validate(self) -> None:
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
        retention_days=_int("MEMGRES_RETENTION_DAYS", 0),
        renew_on_read=_bool("MEMGRES_RENEW_ON_READ", True),
        namespaces_enabled=_bool("MEMGRES_NAMESPACES", False),
        token=_str("MEMGRES_TOKEN", ""),
        tree_enabled=_bool("MEMGRES_TREE", True),
        require_parent=_bool("MEMGRES_REQUIRE_PARENT", False),
        history_enabled=_bool("MEMGRES_HISTORY", True),
        fts_language=_str("MEMGRES_FTS_LANGUAGE", "simple"),
        vector_backend=_str("MEMGRES_VECTOR_BACKEND", "pgvector"),
        embed_provider=_str("MEMGRES_EMBED_PROVIDER", "none"),
        embed_model=_str("MEMGRES_EMBED_MODEL", ""),
        embed_dim=_int("MEMGRES_EMBED_DIM", 0),
        embed_api_key=_str("MEMGRES_EMBED_API_KEY", ""),
        embed_api_base=_str("MEMGRES_EMBED_API_BASE", ""),
        database_url=_str("MEMGRES_DATABASE_URL", ""),
    )
    cfg.validate()
    return cfg
