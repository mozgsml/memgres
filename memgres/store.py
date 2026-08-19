"""The store: create / edit / move / read / forget a memory, with history.

One memory = one mutable body plus metadata (`tags`, tree `path`, TTL). You
change it by sending a whole new body **or** a unified diff; a diff must carry
the `base_hash` it was cut against, so a stale diff is rejected with
:class:`Conflict` (optimistic concurrency, the 409 an HTTP layer maps to).

Every state change appends one hash-chained row to ``memory_history`` with
`source`/`reason` provenance. ``forget`` hard-deletes the row and (by cascade)
its whole history — real erasure, not a tombstone.

Tree moves cascade: changing a node's `path` rewrites every descendant's path in
one ``ltree`` update, so the subtree stays consistent. Search lives in
``search.py``; this module owns mutation and retrieval-by-id.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Optional, Sequence

import psycopg

from . import identity
from .config import Config
from .diffing import DiffConflict, apply_diff, byte_len, content_hash, make_diff
from .embeddings import Embedder, get_embedder
from .vector import make_backend


class Conflict(RuntimeError):
    """base_hash didn't match the current body — re-read and retry (HTTP 409)."""


class NotFound(KeyError):
    """No such memory in this namespace."""


class TooLarge(ValueError):
    """A write or resulting body exceeds the configured ceiling."""


class NoParent(ValueError):
    """MEMGRES_REQUIRE_PARENT is on and the node's parent path doesn't exist."""


class ReplaceNotFound(ValueError):
    """A substring `replace`'s `old` text does not occur in the current body."""


class AmbiguousReplace(ValueError):
    """A `replace`'s `old` occurs more than once and `replace_all` wasn't set."""


@dataclass
class Memory:
    id: str
    body: str
    content_hash: str
    tags: List[str]
    path: Optional[str]
    seq: int
    created_at: object
    updated_at: object
    expires_at: object
    title: str = ""

    def to_dict(self, *, stringify_dates: bool = False) -> dict:
        """Serialize for an API layer. ``stringify_dates`` str()-coerces the
        timestamps (the MCP layer needs plain strings; FastAPI JSON-encodes
        datetimes itself, so the HTTP layer passes them through raw)."""
        def d(v):
            return (str(v) if v is not None else None) if stringify_dates else v
        return {"id": self.id, "content_hash": self.content_hash, "body": self.body,
                "title": self.title, "tags": self.tags, "path": self.path,
                "seq": self.seq, "created_at": d(self.created_at),
                "updated_at": d(self.updated_at), "expires_at": d(self.expires_at)}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fold(digest: str, label: str, *fields: str) -> str:
    """Fold extra fields into a running digest through a DOMAIN-SEPARATED outer
    hash rather than appending to the flat field list. `source`/`reason`/`title`
    are client free text that may contain the ``\\x1f`` separator; concatenating
    them raw would let a crafted field absorb a delimiter and collide two
    logically different rows. So each field is first reduced to its own
    fixed-width sha (``_sha(f)``) before the join: the label namespaces the
    dimension, and every joined part (label, the inner digest, each field-hash) is
    now fixed-width with no client bytes spanning a boundary — injective across
    both dimensions AND the fields within one."""
    parts = [label, digest, *(_sha(f) for f in fields)]
    return _sha("\x1f".join(parts))


def _row_hash(prev: Optional[str], memory_id: str, seq: int, op: str,
              diff: Optional[str], hash_after: Optional[str],
              path_after: Optional[str], tags_after: Optional[Sequence[str]],
              source: Optional[str], reason: Optional[str],
              author_user_id: Optional[str] = None,
              author_token_id: Optional[str] = None,
              title_before: Optional[str] = None,
              title_after: Optional[str] = None) -> str:
    parts = [prev or "", memory_id, str(seq), op, diff or "", hash_after or "",
             path_after or "", ",".join(tags_after or []), source or "", reason or ""]
    h = _sha("\x1f".join(parts))
    # Each optional dimension folds in ONLY when it was touched on this row. A row
    # that touched neither title nor author — which includes EVERY row written
    # before those features — returns the base digest unchanged and still
    # verifies. Order (title then author) is fixed so compute and verify agree.
    if title_before is not None or title_after is not None:
        h = _fold(h, "memgres.title.v1", title_before or "", title_after or "")
    if author_user_id:
        h = _fold(h, "memgres.author.v1", author_user_id, author_token_id or "")
    return h


class Store:
    def __init__(self, cfg: Config, embedder: Optional[Embedder] = None,
                 conn: Optional["psycopg.Connection"] = None,
                 backend: object = None):
        self.cfg = cfg
        self.embedder = embedder if embedder is not None else get_embedder(cfg)
        # identity is on for open/managed; single mode is one shared space,
        # namespace = '' , no auth.
        self._identity_on = cfg.key_mode != "single"
        self._own_conn = conn is None
        self._conn = conn or psycopg.connect(cfg.database_url or "")
        # Vector backend picks itself from config (pgvector in-row by default,
        # Qdrant out-of-band); None when there's no embedder (lexical-only). A
        # server builds it ONCE and injects it here (a qdrant backend opens a
        # client + does setup round-trips on construction, so per-request is
        # wasteful); library/embedded use lets it default.
        self._vectors = backend if backend is not None else make_backend(cfg, self.embedder)

    def close(self):
        if self._own_conn:
            self._conn.close()

    # ─── namespace resolution / authorization ───────────────────────────────
    def _authorize(self, token: Optional[str], *, space: Optional[str] = None,
                   space_id: Optional[str] = None, need: str = "read",
                   for_write: bool = False):
        """Resolve (token, space) to ``(namespace_id, author)`` and enforce
        ``need`` (read|write|admin). ``author`` is ``(user_id, token_id)`` for the
        authenticated principal — stamped into history on writes — or ``None`` for
        a user-less caller (single mode, or the global-admin env token). In single
        mode there is one shared space ('') and no auth; in open/managed mode a
        token is required and the returned id-string is a namespace uuid."""
        if not self._identity_on:
            return "", None
        token = token or self.cfg.default_token or None
        with self._conn.transaction():
            principal = identity.resolve(self._conn, self.cfg, token)
            nsid, perm = identity.resolve_space(
                self._conn, principal, space=space, space_id=space_id,
                for_write=for_write)
        if not identity.perm_at_least(perm, need):
            raise identity.AuthError(
                f"{need} permission required for this namespace (token grants {perm})")
        # user_id is set by now for any materialized principal (open-mode
        # provisional users are created inside resolve_space on for_write).
        author = (principal.user_id, principal.token_id)
        return nsid, author

    def _expiry_sql(self, ttl_days: Optional[int]) -> str:
        days = ttl_days if ttl_days is not None else self.cfg.retention_days
        return "NULL" if not days or days <= 0 else f"now() + interval '{int(days)} days'"

    def _check_parent(self, cur, ns: str, path: Optional[str]):
        """When MEMGRES_REQUIRE_PARENT is on, a non-root node's parent path must
        already exist as a memory. Root nodes (nlevel 1) have no parent to check."""
        if not (self.cfg.require_parent and path):
            return
        cur.execute(
            "SELECT nlevel(%s::ltree) > 1 AND NOT EXISTS ("
            "  SELECT 1 FROM memory WHERE namespace=%s "
            "  AND path = subpath(%s::ltree, 0, nlevel(%s::ltree)-1))",
            (path, ns, path, path))
        if cur.fetchone()[0]:
            raise NoParent(f"parent of '{path}' does not exist "
                           f"(MEMGRES_REQUIRE_PARENT is on)")

    def _index_now(self, memory_id: str, body: str, ns: str, src_hash: str) -> None:
        """Build this memory's chunk vectors inline, within the write transaction,
        UNLESS the deployment defers to a worker (``embed_dispatch == "async"``).
        Inline keeps semantic recall correct the instant a write commits
        (library/embedded, tests); async leaves the row ``embed_pending`` for a
        worker (in-process or a separate memgres-worker) and this is a no-op."""
        if self.cfg.embed_dispatch == "async" or self._vectors is None:
            return
        from .indexing import index_memory
        index_memory(self._conn, self.cfg, self.embedder, self._vectors,
                     memory_id, body, ns, src_hash)

    # ─── write: create, replace, diff, move, retag (one entrypoint) ─────────
    def write(self, token: Optional[str] = None, *, id: Optional[str] = None,
              body: Optional[str] = None, diff: Optional[str] = None,
              base_hash: Optional[str] = None, path: Optional[str] = None,
              tags: Optional[Sequence[str]] = None, source: Optional[str] = None,
              reason: Optional[str] = None, ttl_days: Optional[int] = None,
              title: Optional[str] = None,
              replace: Optional[Sequence[str]] = None, replace_all: bool = False,
              space: Optional[str] = None, space_id: Optional[str] = None) -> Memory:
        if replace is not None and id is None:
            raise ValueError("replace edits an existing memory — pass its id")
        with self._conn.transaction():
            # authorize inside the tx so a lazily-created user/namespace commits
            # atomically with the write (or rolls back together on failure).
            ns, author = self._authorize(token, space=space, space_id=space_id,
                                         need="write", for_write=True)
            self._check_provenance_size(source, reason)
            self._check_title_size(title)
            if id is None:
                return self._create(ns, author, body, path, tags, source, reason,
                                    ttl_days, title)
            return self._update(ns, author, id, body, diff, base_hash, path, tags,
                                source, reason, ttl_days, title=title,
                                replace=replace, replace_all=replace_all)

    def _check_write_size(self, payload: Optional[str]):
        if payload is not None and byte_len(payload) > self.cfg.max_write_bytes:
            raise TooLarge(
                f"write is {byte_len(payload)}B > MEMGRES_MAX_WRITE_BYTES "
                f"{self.cfg.max_write_bytes}")

    def _check_body_size(self, body: str):
        if byte_len(body) > self.cfg.max_body_bytes:
            raise TooLarge(
                f"body would be {byte_len(body)}B > MEMGRES_MAX_BODY_BYTES "
                f"{self.cfg.max_body_bytes}")

    def _check_provenance_size(self, source: Optional[str], reason: Optional[str]):
        if source is not None and byte_len(source) > self.cfg.max_source_bytes:
            raise TooLarge(
                f"source is {byte_len(source)}B > MEMGRES_MAX_SOURCE_BYTES "
                f"{self.cfg.max_source_bytes}")
        if reason is not None and byte_len(reason) > self.cfg.max_reason_bytes:
            raise TooLarge(
                f"reason is {byte_len(reason)}B > MEMGRES_MAX_REASON_BYTES "
                f"{self.cfg.max_reason_bytes}")

    def _check_title_size(self, title: Optional[str]):
        if title is not None and byte_len(title) > self.cfg.max_title_bytes:
            raise TooLarge(
                f"title is {byte_len(title)}B > MEMGRES_MAX_TITLE_BYTES "
                f"{self.cfg.max_title_bytes}")

    def _create(self, ns, author, body, path, tags, source, reason, ttl_days,
                title=None) -> Memory:
        if body is None:
            raise ValueError("create needs a body (diffs apply to an existing memory)")
        self._check_write_size(body)
        self._check_body_size(body)
        chash = content_hash(body)
        tags = list(tags or [])
        title = title or ""
        cur = self._conn.cursor()
        self._check_parent(cur, ns, path)
        pending = self._vectors is not None   # chunks need (async or inline) embedding
        cur.execute(
            f"""INSERT INTO memory (namespace, body, content_hash, tags, path, fts,
                    title, title_fts, seq, embed_pending, expires_at)
                VALUES (%s, %s, %s, %s, %s::ltree,
                        to_tsvector(%s::regconfig, %s),
                        %s, to_tsvector(%s::regconfig, %s),
                        1, %s, {self._expiry_sql(ttl_days)})
                RETURNING id, created_at, updated_at, expires_at""",
            [ns, body, chash, tags, path, self.cfg.fts_language, body,
             title, self.cfg.fts_language, title, pending],
        )
        mid, created, updated, expires = cur.fetchone()
        if pending:
            self._index_now(str(mid), body, ns, chash)   # inline unless async
        # store create as a diff-from-empty so the whole history is a self-contained
        # chain (empty → current), replayable forward for reconstruct/annotate. A
        # non-empty title at creation is audited (title_before None → title_after).
        self._append_history(str(mid), 1, "create", make_diff("", body), None, chash,
                             None, path, None, tags, source, reason, author,
                             title_after=(title or None))
        return Memory(str(mid), body, chash, tags, path, 1, created, updated,
                      expires, title)

    def _load(self, cur, ns, id) -> tuple:
        cur.execute(
            "SELECT body, content_hash, tags, path::text, seq, title FROM memory "
            "WHERE id=%s AND namespace=%s FOR UPDATE", (id, ns))
        row = cur.fetchone()
        if row is None:
            raise NotFound(id)
        return row  # body, content_hash, tags, path, seq, title

    def _resolve_new_body(self, cur_body, cur_hash, *, body, diff, base_hash,
                          replace=None, replace_all=False):
        """Decide the new body text + the body-op from whichever edit form was
        given — a unified ``diff``, a substring ``replace`` (old, new), a whole
        ``body``, or none (metadata-only). Returns ``(new_body, op)`` with ``op``
        in ``{"diff", "replace", None}``.

        This is the ONLY place that turns an edit form into ``new_body``: OCC
        (``base_hash``) and the incoming-payload size limit are enforced here, and
        everything downstream is a single path that recomputes the canonical diff
        from ``cur_body → new_body`` — so every form is line-attributable history
        with no parallel path."""
        if diff is not None:
            if base_hash is None:
                raise ValueError("a diff must carry base_hash (the body it was cut from)")
            if base_hash != cur_hash:
                raise Conflict(f"stale diff: base {base_hash[:12]} != current {cur_hash[:12]}")
            self._check_write_size(diff)
            new_body = apply_diff(cur_body, diff)
            # A diff that leaves the body identical applied nothing meaningful —
            # never bump seq on a silent no-op (belt to apply_diff's malformed
            # guard: also catches an empty or net-zero diff).
            if new_body == cur_body:
                raise DiffConflict("diff applied but changed nothing — empty or no-op diff")
            return new_body, "diff"
        if replace is not None:
            # Content-addressed edit (Edit-tool ergonomics): the server finds `old`
            # and rewrites it, so the client never hand-builds a unified diff and
            # never ships the whole body — a >MAX_WRITE_BYTES body stays editable
            # because only old+new cross the wire. base_hash is optional here: a
            # unique `old` match against the FOR-UPDATE-locked body already guards
            # drift; when supplied it adds strict OCC.
            old, new = replace
            if not old:
                raise ValueError("replace `old` must be a non-empty string")
            if base_hash is not None and base_hash != cur_hash:
                raise Conflict(f"stale replace: base {base_hash[:12]} != current {cur_hash[:12]}")
            count = cur_body.count(old)
            if count == 0:
                raise ReplaceNotFound(f"replace text not found in body: {old[:60]!r}")
            if count > 1 and not replace_all:
                raise AmbiguousReplace(
                    f"replace text occurs {count}× — pass replace_all, or add "
                    f"surrounding context to make it unique: {old[:60]!r}")
            self._check_write_size(old + new)   # cap old+new, NOT the whole body
            # Bound the RESULT before materializing it: replace_all over a
            # many-occurrence body can multiply a 16 KB `new` by thousands of
            # hits, so `cur_body.replace()` alone could allocate gigabytes (then
            # hash + diff them) before the after-the-fact body-size check. Project
            # the byte size from `count` and reject up front.
            hits = count if replace_all else 1
            projected = byte_len(cur_body) + hits * (byte_len(new) - byte_len(old))
            if projected > self.cfg.max_body_bytes:
                raise TooLarge(
                    f"replace would grow the body to ~{projected}B > "
                    f"MEMGRES_MAX_BODY_BYTES {self.cfg.max_body_bytes}")
            new_body = (cur_body.replace(old, new) if replace_all
                        else cur_body.replace(old, new, 1))
            if new_body == cur_body:
                raise DiffConflict("replace changed nothing (old == new)")
            return new_body, "diff"             # lowers to the canonical diff path
        if body is not None:
            if base_hash is not None and base_hash != cur_hash:
                raise Conflict(f"stale write: base {base_hash[:12]} != current {cur_hash[:12]}")
            self._check_write_size(body)
            # op "replace" = a whole-body swap; the substring `replace` form above
            # returns "diff" (it lowers to the canonical diff path).
            return body, "replace"
        return cur_body, None             # metadata-only

    def _update(self, ns, author, id, body, diff, base_hash, path, tags,
                source, reason, ttl_days, *, title=None,
                replace=None, replace_all=False) -> Memory:
        cur = self._conn.cursor()
        cur_body, cur_hash, cur_tags, cur_path, seq, cur_title = self._load(cur, ns, id)

        new_body, op = self._resolve_new_body(
            cur_body, cur_hash, body=body, diff=diff, base_hash=base_hash,
            replace=replace, replace_all=replace_all)

        new_hash = content_hash(new_body)
        new_path = path if path is not None else cur_path
        new_tags = list(tags) if tags is not None else list(cur_tags)
        new_title = title if title is not None else cur_title
        body_changed = new_hash != cur_hash
        path_changed = new_path != cur_path
        tags_changed = new_tags != list(cur_tags)
        title_changed = new_title != cur_title

        if op is None:  # nothing content-y: classify the metadata change
            if path_changed:
                op = "move"
            elif tags_changed:
                op = "retag"
            elif title_changed:
                op = "retitle"
            else:
                # pure touch: renew TTL, no history row
                cur.execute(
                    f"UPDATE memory SET updated_at=now(), expires_at={self._expiry_sql(ttl_days)} "
                    "WHERE id=%s", (id,))
                return self.get(None, id, _ns=ns, renew=False)

        if body_changed:
            self._check_body_size(new_body)

        if path_changed:
            self._check_parent(cur, ns, new_path)

        # a path change cascades to the whole subtree (keep ltree consistent)
        if path_changed and cur_path is not None:
            cur.execute(
                "UPDATE memory SET path = %s::ltree || subpath(path, nlevel(%s::ltree)) "
                "WHERE namespace=%s AND path <@ %s::ltree AND id <> %s",
                (new_path, cur_path, ns, cur_path, id))

        cur.execute(
            f"""UPDATE memory SET body=%s, content_hash=%s, tags=%s, path=%s::ltree,
                    fts=to_tsvector(%s::regconfig, %s),
                    title=%s, title_fts=to_tsvector(%s::regconfig, %s),
                    seq=seq+1, updated_at=now(),
                    embed_pending=(embed_pending OR %s),
                    expires_at={self._expiry_sql(ttl_days)}
                WHERE id=%s
                RETURNING seq, created_at, updated_at, expires_at""",
            [new_body, new_hash, new_tags, new_path,
             self.cfg.fts_language, new_body,
             new_title, self.cfg.fts_language, new_title,
             (body_changed and self._vectors is not None), id])
        new_seq, created, updated, expires = cur.fetchone()
        if body_changed and self._vectors is not None:
            # only a body change re-chunks; path/tag/title edits leave vectors be.
            self._index_now(str(id), new_body, ns, new_hash)   # inline unless async
        # store the canonical diff (recomputed) even for a whole-body replace, so
        # every body change is line-attributable and the chain stays replayable.
        stored_diff = make_diff(cur_body, new_body) if body_changed else None
        self._append_history(str(id), new_seq, op, stored_diff,
                             cur_hash, new_hash,
                             cur_path if path_changed else None,
                             new_path if path_changed else None,
                             list(cur_tags) if tags_changed else None,
                             new_tags if tags_changed else None,
                             source, reason, author,
                             title_before=cur_title if title_changed else None,
                             title_after=new_title if title_changed else None)
        return Memory(str(id), new_body, new_hash, new_tags, new_path, new_seq,
                      created, updated, expires, new_title)

    def _append_history(self, memory_id, seq, op, diff, hash_before, hash_after,
                        path_before, path_after, tags_before, tags_after,
                        source, reason, author=None, *,
                        title_before=None, title_after=None):
        author_user_id, author_token_id = author or (None, None)
        cur = self._conn.cursor()
        cur.execute("SELECT row_hash FROM memory_history WHERE memory_id=%s "
                    "ORDER BY seq DESC LIMIT 1", (memory_id,))
        prev = cur.fetchone()
        prev_hash = prev[0] if prev else None
        rhash = _row_hash(prev_hash, memory_id, seq, op, diff, hash_after,
                          path_after, tags_after, source, reason,
                          author_user_id, author_token_id,
                          title_before, title_after)
        cur.execute(
            """INSERT INTO memory_history (memory_id, seq, op, diff, hash_before,
                   hash_after, path_before, path_after, tags_before, tags_after,
                   source, reason, author_user_id, author_token_id,
                   title_before, title_after, prev_row_hash, row_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s::ltree,%s::ltree,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (memory_id, seq, op, diff, hash_before, hash_after, path_before,
             path_after, tags_before, tags_after, source, reason,
             author_user_id, author_token_id, title_before, title_after,
             prev_hash, rhash))

    # ─── recall: lexical / semantic / hybrid ────────────────────────────────
    def recall(self, token: Optional[str], query: str, *, k: int = 10,
               tags: Optional[Sequence[str]] = None,
               path_prefix: Optional[str] = None, mode: str = "auto",
               match: Optional[str] = None,
               snippet: Optional[bool] = None, full_body: Optional[bool] = None,
               space: Optional[str] = None, space_id: Optional[str] = None):
        from .search import recall as _recall
        ns, _ = self._authorize(token, space=space, space_id=space_id, need="read")
        return _recall(self._conn, self.cfg, self.embedder, ns,
                       query, k=k, tags=tags, path_prefix=path_prefix, mode=mode,
                       match=match, backend=self._vectors,
                       snippet=snippet, full_body=full_body)

    # ─── find: locate by title (+ tags), no body ────────────────────────────
    def find(self, token: Optional[str], query: str, *, k: int = 10,
             tags: Optional[Sequence[str]] = None,
             path_prefix: Optional[str] = None, match: Optional[str] = None,
             space: Optional[str] = None, space_id: Optional[str] = None) -> List[dict]:
        """Locate memories whose curated `title` matches — a light "where is it"
        search over titles + tags, never the body. Returns light rows; works
        without an embedder. See ``search.find``."""
        from .search import find as _find
        ns, _ = self._authorize(token, space=space, space_id=space_id, need="read")
        return _find(self._conn, self.cfg, ns, query, tags=tags,
                     path_prefix=path_prefix, k=k, match=match)

    # ─── list: enumerate a subtree (no query, no ranking) ───────────────────
    def list(self, token: Optional[str], *, path_prefix: Optional[str] = None,
             tags: Optional[Sequence[str]] = None, limit: int = 50,
             offset: int = 0, space: Optional[str] = None,
             space_id: Optional[str] = None) -> List[dict]:
        """Browse (enumerate) memories under a subtree — NOT a search: no FTS, no
        vectors, no scoring. Rows are ordered by path so a subtree reads in tree
        order. ``build_filters`` scopes by namespace + not-expired + tags +
        subtree, so this is multi-tenant safe by construction."""
        from .vector.base import build_filters
        ns, _ = self._authorize(token, space=space, space_id=space_id, need="read")
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where, params = build_filters(ns, tags, path_prefix)
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, path::text, tags, title, "
            "left(split_part(body, E'\\n', 1), %s) AS preview, "
            "created_at, updated_at "
            f"FROM memory WHERE {where} ORDER BY path, id LIMIT %s OFFSET %s",
            [self.cfg.list_preview_chars] + params + [limit, offset])
        cols = ["id", "path", "tags", "title", "preview", "created_at", "updated_at"]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["id"] = str(d["id"])
            d["tags"] = list(d["tags"]) if d["tags"] is not None else []
            rows.append(d)
        return rows

    # ─── convenience: move ──────────────────────────────────────────────────
    def move(self, token: Optional[str], id: str, new_path: str,
             *, source: Optional[str] = None, reason: Optional[str] = None,
             space: Optional[str] = None, space_id: Optional[str] = None) -> Memory:
        return self.write(token, id=id, path=new_path, source=source, reason=reason,
                          space=space, space_id=space_id)

    # ─── read ───────────────────────────────────────────────────────────────
    def get(self, token: Optional[str], id: str, *, renew: bool = True,
            _ns: Optional[str] = None, space: Optional[str] = None,
            space_id: Optional[str] = None) -> Memory:
        ns = _ns if _ns is not None else self._authorize(
            token, space=space, space_id=space_id, need="read")[0]
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, body, content_hash, tags, path::text, seq, created_at, "
            "updated_at, expires_at, title FROM memory WHERE id=%s AND namespace=%s",
            (id, ns))
        row = cur.fetchone()
        if row is None:
            raise NotFound(id)
        if renew and self.cfg.renew_on_read and self.cfg.retention_days > 0:
            with self._conn.transaction():
                cur.execute(
                    f"UPDATE memory SET expires_at={self._expiry_sql(None)} WHERE id=%s",
                    (id,))
        return Memory(str(row[0]), row[1], row[2], list(row[3]), row[4], row[5],
                      row[6], row[7], row[8], row[9])

    def history(self, token: Optional[str], id: str, *, space: Optional[str] = None,
                space_id: Optional[str] = None) -> List[dict]:
        ns, _ = self._authorize(token, space=space, space_id=space_id, need="read")
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM memory WHERE id=%s AND namespace=%s", (id, ns))
        if cur.fetchone() is None:
            raise NotFound(id)
        # LEFT JOIN so a since-deleted author still reads back as its bare id
        # (author_name NULL) — the audit stamp survives the user row.
        cur.execute(
            "SELECT h.seq, h.op, h.diff, h.hash_before, h.hash_after, "
            "h.path_before::text, h.path_after::text, h.tags_before, h.tags_after, "
            "h.source, h.reason, h.author_user_id::text, h.author_token_id::text, "
            "NULLIF(u.name, '') AS author_name, h.title_before, h.title_after, "
            "h.prev_row_hash, h.row_hash, h.created_at FROM memory_history h "
            "LEFT JOIN app_user u ON u.id = h.author_user_id "
            "WHERE h.memory_id=%s ORDER BY h.seq", (id,))
        cols = ["seq", "op", "diff", "hash_before", "hash_after", "path_before",
                "path_after", "tags_before", "tags_after", "source", "reason",
                "author_user_id", "author_token_id", "author_name",
                "title_before", "title_after",
                "prev_row_hash", "row_hash", "created_at"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def annotate(self, token: Optional[str], id: str,
                 upto_seq: Optional[int] = None,
                 lines: Optional[Sequence[int]] = None, *,
                 space: Optional[str] = None, space_id: Optional[str] = None) -> List[dict]:
        """Blame: the body with each line tagged by who last changed it. Pass
        `lines` (1-based line numbers) to return only those lines."""
        from .blame import annotate as _annotate
        return _annotate(self.history(token, id, space=space, space_id=space_id),
                         upto_seq, lines)

    def annotate_grouped(self, token: Optional[str], id: str,
                         upto_seq: Optional[int] = None,
                         include_text: bool = True, *,
                         space: Optional[str] = None,
                         space_id: Optional[str] = None) -> List[dict]:
        """Blame as runs: consecutive same-author lines collapse into one block.
        `include_text=False` returns a pure ownership map (ranges, no body)."""
        from .blame import annotate_grouped as _grouped
        return _grouped(self.history(token, id, space=space, space_id=space_id),
                        upto_seq, include_text)

    def reconstruct(self, token: Optional[str], id: str,
                    upto_seq: Optional[int] = None, *,
                    space: Optional[str] = None, space_id: Optional[str] = None) -> str:
        """The exact body text at a past version (default current)."""
        from .blame import reconstruct as _reconstruct
        return _reconstruct(self.history(token, id, space=space, space_id=space_id),
                            upto_seq)

    def verify_history(self, token: Optional[str], id: str, *,
                       space: Optional[str] = None,
                       space_id: Optional[str] = None) -> bool:
        """Recompute the chain; True if untampered."""
        rows = self.history(token, id, space=space, space_id=space_id)
        prev = None
        for r in rows:
            expect = _row_hash(prev, id, r["seq"], r["op"], r["diff"],
                               r["hash_after"], r["path_after"], r["tags_after"],
                               r["source"], r["reason"],
                               r["author_user_id"], r["author_token_id"],
                               r["title_before"], r["title_after"])
            if expect != r["row_hash"] or (r["prev_row_hash"] or None) != prev:
                return False
            prev = r["row_hash"]
        return True

    # ─── forget: real erasure ───────────────────────────────────────────────
    def forget(self, token: Optional[str], id: str, *, space: Optional[str] = None,
               space_id: Optional[str] = None) -> bool:
        ns, _ = self._authorize(token, space=space, space_id=space_id, need="write")
        with self._conn.transaction():
            cur = self._conn.cursor()
            cur.execute("DELETE FROM memory WHERE id=%s AND namespace=%s", (id, ns))
            deleted = cur.rowcount > 0
        if deleted and self._vectors is not None:
            # drop this memory's chunk vectors (pgvector: FK-cascaded already;
            # qdrant: an out-of-band collection, so this is the real cleanup there)
            self._vectors.delete_chunks(self._conn, id, ns)
        return deleted

    def purge_expired(self) -> int:
        with self._conn.transaction():
            cur = self._conn.cursor()
            cur.execute("DELETE FROM memory WHERE expires_at IS NOT NULL "
                        "AND expires_at < now()")
            return cur.rowcount
