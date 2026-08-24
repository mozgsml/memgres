"""memgres — versioned document memory for AI agents, backed by one Postgres.

Quick start::

    from memgres import Store, load_config, migrate
    import psycopg

    cfg = load_config()                     # reads MEMGRES_* env
    conn = psycopg.connect(cfg.database_url)
    migrate(conn, cfg)                       # idempotent; stamps embed model/dim

    store = Store(cfg, conn=conn)
    m = store.write(body="remember this\n", tags=["note"], source="me")
    store.write(id=m.id, diff=patch, base_hash=m.content_hash)   # authored edit
    hits = store.recall(None, "what did I remember?")            # lexical or semantic
"""

from ._version import __version__
from .config import Config, load as load_config
from .diffing import apply_diff, content_hash, make_diff, DiffConflict
from .embeddings import Embedder, get_embedder
from .schema import migrate, SchemaMismatch, SCHEMA_VERSION
from .search import Hit, recall
from .blame import annotate, annotate_grouped, reconstruct, replay
from .store import Store, Memory, Conflict, NotFound, TooLarge, NoParent
from .identity import (
    Principal, AuthError, SpaceNotFound,
    resolve, resolve_space, new_token, valid_format,
    create_user, create_namespace, list_spaces,
    list_users, list_namespaces, list_members,
    issue_token, register_token, revoke_token, list_tokens, token_owner,
    request_access, approve_request, deny_request, list_requests,
)

__all__ = [
    "__version__",
    "Config", "load_config",
    "Store", "Memory", "Conflict", "NotFound", "TooLarge", "NoParent",
    "make_diff", "apply_diff", "content_hash", "DiffConflict",
    "Embedder", "get_embedder",
    "migrate", "SchemaMismatch", "SCHEMA_VERSION",
    "Hit", "recall",
    "annotate", "annotate_grouped", "reconstruct", "replay",
    "Principal", "AuthError", "SpaceNotFound",
    "resolve", "resolve_space", "new_token", "valid_format",
    "create_user", "create_namespace", "list_spaces",
    "list_users", "list_namespaces", "list_members",
    "issue_token", "register_token", "revoke_token", "list_tokens", "token_owner",
    "request_access", "approve_request", "deny_request", "list_requests",
]
