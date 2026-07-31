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


def _row_hash(prev: Optional[str], memory_id: str, seq: int, op: str,
              diff: Optional[str], hash_after: Optional[str],
              path_after: Optional[str], tags_after: Optional[Sequence[str]],
              source: Optional[str], reason: Optional[str]) -> str:
    h = hashlib.sha256()
    parts = [prev or "", memory_id, str(seq), op, diff or "", hash_after or "",
             path_after or "", ",".join(tags_after or []), source or "", reason or ""]
    h.update("\x1f".join(parts).encode("utf-8"))
    return h.hexdigest()


class Store:
    def __init__(self, cfg: Config, embedder: Optional[Embedder] = None,
                 conn: Optional["psycopg.Connection"] = None):
        self.cfg = cfg
        self.embedder = embedder if embedder is not None else get_embedder(cfg)
        # identity is on for open/managed; single mode is one shared space,
        # namespace = '' , no auth.
        self._identity_on = cfg.key_mode != "single"
        self._own_conn = conn is None
        self._conn = conn or psycopg.connect(cfg.database_url or "")
        # Vector backend picks itself from config (pgvector in-row by default,
        # Qdrant out-of-band); None when there's no embedder (lexical-only).
        self._vectors = make_backend(cfg, self.embedder)

    def close(self):
        if self._own_conn:
            self._conn.close()

    # ─── namespace resolution / authorization ───────────────────────────────
    def _authorize(self, token: Optional[str], *, space: Optional[str] = None,
                   space_id: Optional[str] = None, need: str = "read",
                   for_write: bool = False) -> str:
        """Resolve (token, space) to the namespace id-string to operate in, and
        enforce ``need`` (read|write|admin). In single mode there is one shared
        space ('') and no auth. In open/managed mode a token is required; the
        returned string is a namespace uuid."""
        if not self._identity_on:
            return ""
        token = token or self.cfg.token or None
        with self._conn.transaction():
            principal = identity.resolve(self._conn, self.cfg, token)
            nsid, perm = identity.resolve_space(
                self._conn, principal, space=space, space_id=space_id,
                for_write=for_write)
        if not identity.perm_at_least(perm, need):
            raise identity.AuthError(
                f"{need} permission required for this namespace (token grants {perm})")
        return nsid

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

    def _raw_vec(self, body: str) -> "Optional[list]":
        if not self.embedder:
            return None
        return self.embedder.embed_documents([body])[0]

    # ─── write: create, replace, diff, move, retag (one entrypoint) ─────────
    def write(self, token: Optional[str] = None, *, id: Optional[str] = None,
              body: Optional[str] = None, diff: Optional[str] = None,
              base_hash: Optional[str] = None, path: Optional[str] = None,
              tags: Optional[Sequence[str]] = None, source: Optional[str] = None,
              reason: Optional[str] = None, ttl_days: Optional[int] = None,
              space: Optional[str] = None, space_id: Optional[str] = None) -> Memory:
        with self._conn.transaction():
            # authorize inside the tx so a lazily-created user/namespace commits
            # atomically with the write (or rolls back together on failure).
            ns = self._authorize(token, space=space, space_id=space_id,
                                 need="write", for_write=True)
            self._check_provenance_size(source, reason)
            if id is None:
                return self._create(ns, body, path, tags, source, reason, ttl_days)
            return self._update(ns, id, body, diff, base_hash, path, tags,
                                source, reason, ttl_days)

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

    def _create(self, ns, body, path, tags, source, reason, ttl_days) -> Memory:
        if body is None:
            raise ValueError("create needs a body (diffs apply to an existing memory)")
        self._check_write_size(body)
        self._check_body_size(body)
        chash = content_hash(body)
        tags = list(tags or [])
        cur = self._conn.cursor()
        self._check_parent(cur, ns, path)
        cur.execute(
            f"""INSERT INTO memory (namespace, body, content_hash, tags, path, fts,
                    seq, expires_at)
                VALUES (%s, %s, %s, %s, %s::ltree,
                        to_tsvector(%s::regconfig, %s),
                        1, {self._expiry_sql(ttl_days)})
                RETURNING id, created_at, updated_at, expires_at""",
            [ns, body, chash, tags, path, self.cfg.fts_language, body],
        )
        mid, created, updated, expires = cur.fetchone()
        if self._vectors is not None:
            # pgvector: a second UPDATE in this same tx; qdrant: point upsert.
            self._vectors.upsert_doc(self._conn, str(mid), self._raw_vec(body), ns)
        # store create as a diff-from-empty so the whole history is a self-contained
        # chain (empty → current), replayable forward for reconstruct/annotate.
        self._append_history(str(mid), 1, "create", make_diff("", body), None, chash,
                             None, path, None, tags, source, reason)
        return Memory(str(mid), body, chash, tags, path, 1, created, updated, expires)

    def _load(self, cur, ns, id) -> tuple:
        cur.execute(
            "SELECT body, content_hash, tags, path::text, seq FROM memory "
            "WHERE id=%s AND namespace=%s FOR UPDATE", (id, ns))
        row = cur.fetchone()
        if row is None:
            raise NotFound(id)
        return row  # body, content_hash, tags, path, seq

    def _update(self, ns, id, body, diff, base_hash, path, tags,
                source, reason, ttl_days) -> Memory:
        cur = self._conn.cursor()
        cur_body, cur_hash, cur_tags, cur_path, seq = self._load(cur, ns, id)

        # decide the new body
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
            op = "diff"
        elif body is not None:
            if base_hash is not None and base_hash != cur_hash:
                raise Conflict(f"stale replace: base {base_hash[:12]} != current {cur_hash[:12]}")
            self._check_write_size(body)
            new_body = body
            op = "replace"
        else:
            new_body = cur_body           # metadata-only
            op = None

        new_hash = content_hash(new_body)
        new_path = path if path is not None else cur_path
        new_tags = list(tags) if tags is not None else list(cur_tags)
        body_changed = new_hash != cur_hash
        path_changed = new_path != cur_path
        tags_changed = new_tags != list(cur_tags)

        if op is None:  # nothing content-y: classify the metadata change
            if path_changed:
                op = "move"
            elif tags_changed:
                op = "retag"
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
                    seq=seq+1, updated_at=now(), expires_at={self._expiry_sql(ttl_days)}
                WHERE id=%s
                RETURNING seq, created_at, updated_at, expires_at""",
            [new_body, new_hash, new_tags, new_path,
             self.cfg.fts_language, new_body, id])
        new_seq, created, updated, expires = cur.fetchone()
        if body_changed and self._vectors is not None:
            # pgvector: a second UPDATE in this same tx; qdrant: point upsert.
            self._vectors.upsert_doc(self._conn, str(id), self._raw_vec(new_body), ns)
        # store the canonical diff (recomputed) even for a whole-body replace, so
        # every body change is line-attributable and the chain stays replayable.
        stored_diff = make_diff(cur_body, new_body) if body_changed else None
        self._append_history(str(id), new_seq, op, stored_diff,
                             cur_hash, new_hash,
                             cur_path if path_changed else None,
                             new_path if path_changed else None,
                             list(cur_tags) if tags_changed else None,
                             new_tags if tags_changed else None,
                             source, reason)
        return Memory(str(id), new_body, new_hash, new_tags, new_path, new_seq,
                      created, updated, expires)

    def _append_history(self, memory_id, seq, op, diff, hash_before, hash_after,
                        path_before, path_after, tags_before, tags_after,
                        source, reason):
        cur = self._conn.cursor()
        cur.execute("SELECT row_hash FROM memory_history WHERE memory_id=%s "
                    "ORDER BY seq DESC LIMIT 1", (memory_id,))
        prev = cur.fetchone()
        prev_hash = prev[0] if prev else None
        rhash = _row_hash(prev_hash, memory_id, seq, op, diff, hash_after,
                          path_after, tags_after, source, reason)
        cur.execute(
            """INSERT INTO memory_history (memory_id, seq, op, diff, hash_before,
                   hash_after, path_before, path_after, tags_before, tags_after,
                   source, reason, prev_row_hash, row_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s::ltree,%s::ltree,%s,%s,%s,%s,%s,%s)""",
            (memory_id, seq, op, diff, hash_before, hash_after, path_before,
             path_after, tags_before, tags_after, source, reason, prev_hash, rhash))

    # ─── recall: lexical / semantic / hybrid ────────────────────────────────
    def recall(self, token: Optional[str], query: str, *, k: int = 10,
               tags: Optional[Sequence[str]] = None,
               path_prefix: Optional[str] = None, mode: str = "auto",
               match: Optional[str] = None,
               snippet: Optional[bool] = None, full_body: Optional[bool] = None,
               space: Optional[str] = None, space_id: Optional[str] = None):
        from .search import recall as _recall
        ns = self._authorize(token, space=space, space_id=space_id, need="read")
        return _recall(self._conn, self.cfg, self.embedder, ns,
                       query, k=k, tags=tags, path_prefix=path_prefix, mode=mode,
                       match=match, backend=self._vectors,
                       snippet=snippet, full_body=full_body)

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
        ns = self._authorize(token, space=space, space_id=space_id, need="read")
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where, params = build_filters(ns, tags, path_prefix)
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, path::text, tags, "
            "left(split_part(body, E'\\n', 1), %s) AS preview, "
            "created_at, updated_at "
            f"FROM memory WHERE {where} ORDER BY path, id LIMIT %s OFFSET %s",
            [self.cfg.list_preview_chars] + params + [limit, offset])
        cols = ["id", "path", "tags", "preview", "created_at", "updated_at"]
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
            token, space=space, space_id=space_id, need="read")
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, body, content_hash, tags, path::text, seq, created_at, "
            "updated_at, expires_at FROM memory WHERE id=%s AND namespace=%s",
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
                      row[6], row[7], row[8])

    def history(self, token: Optional[str], id: str, *, space: Optional[str] = None,
                space_id: Optional[str] = None) -> List[dict]:
        ns = self._authorize(token, space=space, space_id=space_id, need="read")
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM memory WHERE id=%s AND namespace=%s", (id, ns))
        if cur.fetchone() is None:
            raise NotFound(id)
        cur.execute(
            "SELECT seq, op, diff, hash_before, hash_after, path_before::text, "
            "path_after::text, tags_before, tags_after, source, reason, "
            "prev_row_hash, row_hash, created_at FROM memory_history "
            "WHERE memory_id=%s ORDER BY seq", (id,))
        cols = ["seq", "op", "diff", "hash_before", "hash_after", "path_before",
                "path_after", "tags_before", "tags_after", "source", "reason",
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
                               r["source"], r["reason"])
            if expect != r["row_hash"] or (r["prev_row_hash"] or None) != prev:
                return False
            prev = r["row_hash"]
        return True

    # ─── forget: real erasure ───────────────────────────────────────────────
    def forget(self, token: Optional[str], id: str, *, space: Optional[str] = None,
               space_id: Optional[str] = None) -> bool:
        ns = self._authorize(token, space=space, space_id=space_id, need="write")
        with self._conn.transaction():
            cur = self._conn.cursor()
            cur.execute("DELETE FROM memory WHERE id=%s AND namespace=%s", (id, ns))
            deleted = cur.rowcount > 0
        if deleted and self._vectors is not None:
            # drop the out-of-band vector too (no-op for the in-row pgvector backend)
            self._vectors.delete_doc(self._conn, id, ns)
            # and the durable segment cache (pgvector: FK-cascaded already; qdrant:
            # a sibling collection, so this is the actual cleanup there)
            self._vectors.delete_segments(self._conn, id)
        return deleted

    def purge_expired(self) -> int:
        with self._conn.transaction():
            cur = self._conn.cursor()
            cur.execute("DELETE FROM memory WHERE expires_at IS NOT NULL "
                        "AND expires_at < now()")
            return cur.rowcount
