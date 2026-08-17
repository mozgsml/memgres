"""Effective server configuration, exposed read-only for introspection.

An agent shouldn't have to guess the write ceilings, which recall modes are
available, or how memories are embedded. ``server_info`` distills the loaded
``Config`` (plus the live embedder's dimension, if one is built) into a small,
non-sensitive dict. It deliberately carries **no** secrets — no token, no api
key, no database url — so it is safe to return unauthenticated.
"""

from __future__ import annotations

from typing import Optional

from ._version import __version__
from .config import Config
from .schema import SCHEMA_VERSION


def server_info(cfg: Config, embed_dim: Optional[int] = None) -> dict:
    """Effective limits + capabilities, from the loaded config (and the live
    embedder's dimension when available). Never includes secrets.

    ``version`` is the running package version (from code, so an editable/dev
    checkout reports what it's actually running); ``schema_version`` is the DB
    layout this build migrates to."""
    lexical_only = cfg.embed_provider == "none"
    dim = embed_dim if embed_dim is not None else (cfg.embed_dim or None)
    return {
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "limits": {
            "max_body_bytes": cfg.max_body_bytes,
            "max_write_bytes": cfg.max_write_bytes,
            "max_source_bytes": cfg.max_source_bytes,
            "max_reason_bytes": cfg.max_reason_bytes,
        },
        "embed": {
            "provider": cfg.embed_provider,
            "model": cfg.embed_model or None,
            "dim": dim,
        },
        "recall_modes": ["lexical"] if lexical_only
        else ["lexical", "semantic", "hybrid", "auto"],
        "vector_backend": cfg.vector_backend,
        "key_mode": cfg.key_mode,
        "fts_language": cfg.fts_language,
    }
