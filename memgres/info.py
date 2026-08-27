"""Effective server configuration, exposed read-only for introspection.

An agent shouldn't have to guess the write ceilings, how long a memory is kept,
which recall modes are available, or how memories are embedded. ``server_info`` distills the loaded
``Config`` (plus the live embedder's dimension, if one is built) into a small,
non-sensitive dict. It deliberately carries **no** secrets — no token, no api
key, no database url — so it is safe to return unauthenticated.
"""

from __future__ import annotations

from typing import Optional

from ._version import __version__
from .config import Config
from .schema import SCHEMA_VERSION


def _retention_policy(days: int, renew_on_read: bool) -> str:
    """One plain sentence a human (or an agent explaining itself) can quote."""
    if days <= 0:
        return "kept indefinitely — memories do not expire"
    touch = ("any touch, a read included, starts the window again"
             if renew_on_read else "only a write restarts the window")
    return f"deleted {days} days after the last touch ({touch})"


def server_info(cfg: Config, embed_dim: Optional[int] = None) -> dict:
    """Effective limits + capabilities, from the loaded config (and the live
    embedder's dimension when available). Never includes secrets.

    ``version`` is the running package version (from code, so an editable/dev
    checkout reports what it's actually running); ``schema_version`` is the DB
    layout this build migrates to."""
    lexical_only = cfg.embed_provider == "none"
    dim = embed_dim if embed_dim is not None else (cfg.embed_dim or None)
    days = cfg.retention_days
    return {
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "limits": {
            "max_body_bytes": cfg.max_body_bytes,
            "max_write_bytes": cfg.max_write_bytes,
            "max_source_bytes": cfg.max_source_bytes,
            "max_reason_bytes": cfg.max_reason_bytes,
            "max_title_bytes": cfg.max_title_bytes,
            # what a `bodies=true` browse may return in total, so a caller can
            # size its request instead of discovering the cap by hitting it
            "list_bodies_max_bytes": cfg.list_bodies_max_bytes,
        },
        "embed": {
            "provider": cfg.embed_provider,
            "model": cfg.embed_model or None,
            "dim": dim,
        },
        # How long a memory survives, said out loud. A client that cannot see
        # this has to guess whether what it stores today will still be there in a
        # month — and "kept indefinitely" is the answer it guesses WRONG about
        # most often, because nothing in a reply hints that an expiry exists at
        # all. `days: null` is the unlimited case, spelled again in `policy`.
        "retention": {
            "days": days if days > 0 else None,
            "expires": days > 0,
            # A read renewing the window only means anything if there is one.
            "renew_on_read": bool(days > 0 and cfg.renew_on_read),
            "policy": _retention_policy(days, cfg.renew_on_read),
        },
        # What a write MUST carry here. Announced rather than discovered from a
        # refusal: a client that learns the rule by being rejected has already
        # composed the memory, and a rule nobody can see before writing is a rule
        # that gets satisfied with junk on the second attempt.
        "write_requirements": {
            "title": cfg.require_title,
            "fields": list(cfg.required_fields),
        },
        "recall_modes": ["lexical"] if lexical_only
        else ["lexical", "semantic", "hybrid", "auto"],
        "vector_backend": cfg.vector_backend,
        "key_mode": cfg.key_mode,
        "fts_language": cfg.fts_language,
    }
