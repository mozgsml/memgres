"""The store: create / edit / move / read / forget a memory, with history.

One memory = one mutable body plus metadata (`tags`, tree `path`, TTL). You
change it by sending a whole new body **or** a unified diff; a diff must carry
the `base_hash` it was cut against, so a stale diff is rejected with
:class:`Conflict` (optimistic concurrency, the 409 an HTTP layer maps to).

Every state change appends one hash-chained row to ``memory_history`` with
`source`/`reason` provenance. ``forget`` hard-deletes the row and (by cascade)
its whole history — real erasure, not a tombstone.

A memory has two addresses: its `id` and its tree `path`, which is unique within
a namespace. Operations take either (`at=` for the path). A path that a memory
has moved away from still resolves, from the `move` rows in its history — reads
follow it, writes refuse and say where it went, so a stale address cannot quietly
become a second memory beside the first.

Tree moves cascade: changing a node's `path` re-addresses its whole subtree, and
each descendant records that move in its OWN history — a node's address changing
is a change to that node, and it is what lets an old path still be resolved
later. Search lives in ``search.py``; this module owns mutation and
retrieval-by-id.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import psycopg

from . import identity
from .config import Config
from .delimiters import write_warnings
from .lines import parse_line_spec
from .diffing import DiffConflict, apply_diff, byte_len, content_hash, make_diff
from .embeddings import Embedder, get_embedder
from .links import parse_links
from .tags import check_tag_match, normalize_tags
from .vector import make_backend


# The most rows any one call may return. Search and browse share it so a caller
# cannot pick whichever path forgot to clamp.
MAX_RESULTS = 500


class Conflict(RuntimeError):
    """base_hash didn't match the current body — re-read and retry (HTTP 409)."""


class NotFound(KeyError):
    """No such memory in this namespace."""


class TooLarge(ValueError):
    """A write or resulting body exceeds the configured ceiling."""


def _as_date(value: object):
    """Accept a `date` or an ISO `YYYY-MM-DD` string; refuse anything else.

    Transports hand this over as JSON, so a string is the common case. Parsing it
    here rather than letting Postgres cast keeps one spelling for the hash — the
    digest folds the ISO text, and `"2021-3-4"` and `"2021-03-04"` must not be two
    different dates to the chain."""
    import datetime as _dt
    if value is None or isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ValueError(
        f"valid_at must be a date as YYYY-MM-DD (got {value!r}) — it is the day "
        f"the content was last known to be accurate")


class MissingTitle(ValueError):
    """A write stored content without a caption, and the deployment requires one
    (``MEMGRES_REQUIRE_TITLE``, default on).

    Raised BEFORE the write, so the caller retries with a title rather than
    leaving a memory it cannot address later. An untitled memory is not broken,
    it is merely poorer: nothing captions it in a result list, and title-weighted
    ranking has nothing to weigh. Enforcing it at the moment content is written
    is also what migrates an existing corpus — each memory gains a title the next
    time someone actually edits it, with no bulk pass."""


class NoParent(ValueError):
    """MEMGRES_REQUIRE_PARENT is on and the node's parent path doesn't exist."""


class PathMoved(ValueError):
    """The addressed path exists only in history — the memory moved away.

    Carries where it went, because the useful answer to "there is nothing here"
    is "it is over there now". Raised instead of quietly resolving to the moved
    memory (a write meant for one address landing on another) or quietly creating
    a second memory at the vacated one (the silent fork this guards).
    """

    def __init__(self, path: str, memory_id: str, moved_to: Optional[str],
                 message: str):
        super().__init__(message)
        self.path = path
        self.memory_id = memory_id
        self.moved_to = moved_to


class PathTaken(ValueError):
    """Creating at a path another memory already occupies.

    A path is unique within a namespace, so this was always refused — but by a
    raw unique-index violation that named neither the path nor its occupant.
    """

    def __init__(self, path: str, memory_id: str):
        super().__init__(f"'{path}' is already taken by memory {memory_id} — "
                         f"edit it (`at='{path}'`) or choose another path")
        self.path = path
        self.memory_id = memory_id


class ReplaceNotFound(ValueError):
    """A substring `replace`'s `old` text does not occur in the current body."""


class AmbiguousReplace(ValueError):
    """A `replace`'s `old` occurs more than once and `replace_all` wasn't set."""


# The same substring edit is spelled three ways across the ecosystem, and memgres
# is the odd one out — its `replace_` prefix exists to group with `replace_all`.
# An agent with muscle memory for file editors reaches for the others and gets a
# confusing refusal, so all three are accepted and folded to the canon here.
REPLACE_ALIASES = {
    "old": ("replace_old", "old_string", "old_str"),
    "new": ("replace_new", "new_string", "new_str"),
}


def fold_replace_aliases(values: dict) -> dict:
    """Reduce the accepted spellings to ``replace_old``/``replace_new``.

    Two spellings of the same side carrying DIFFERENT text is refused rather than
    resolved: picking one silently would apply an edit the caller did not ask
    for, and this whole family of parameters already has a history of quiet
    damage. Identical values are fine — that is just a client being redundant.
    """
    out = dict(values)
    for side, names in REPLACE_ALIASES.items():
        seen = {n: out.pop(n) for n in names if out.get(n) is not None}
        for n in names:
            out.pop(n, None)
        if not seen:
            continue
        distinct = set(seen.values())
        if len(distinct) > 1:
            spelled = ", ".join(f"{n}={v!r}" for n, v in sorted(seen.items()))
            raise ValueError(
                f"conflicting values for the replacement's {side} text: {spelled}"
                " — they name the same parameter, so send only one")
        out[names[0]] = next(iter(distinct))
    return out


def build_replace(replace_old: Optional[str], replace_new: Optional[str]):
    """Assemble the substring-replace ``(old, new)`` tuple from the two optional
    request fields — the one place both the HTTP and MCP layers turn them into the
    ``replace`` argument, so the rule lives once.

    It distinguishes *not provided* (``None``) from an explicit empty string. Both
    omitted → ``None`` (no replace). Exactly one provided → ``ValueError``: a lone
    ``replace_old`` must not be coerced to ``(old, "")`` and **silently delete**
    the matched text (the regression this guards), and a lone ``replace_new`` has
    no anchor. Both provided → ``(old, new)``; ``new`` may be ``""`` to delete on
    purpose, which is now the *only* way to get a deletion.
    """
    if replace_old is None and replace_new is None:
        return None
    if replace_old is None or replace_new is None:
        missing = "replace_new" if replace_new is None else "replace_old"
        other = "new_string`/`new_str" if replace_new is None else "old_string`/`old_str"
        raise ValueError(
            f"replace needs both replace_old and replace_new — `{missing}` is "
            f"missing (file editors call it `{other}`; those spellings are "
            f"accepted too). Pass replace_new='' to delete the matched text.")
    return (replace_old, replace_new)


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
    # Set only when the caller needs telling something they didn't ask:
    # `created` distinguishes a write that made a new memory from one that
    # edited an existing one, and `moved_from` names the old address a request
    # was resolved through, so a caller writing to a stale path learns of it
    # from the answer instead of forking the memory.
    created: Optional[bool] = None
    moved_from: Optional[str] = None
    # Non-fatal notes about a write that SUCCEEDED — the data is stored, and
    # this is what stops a silent corruption from staying silent.
    warnings: List[str] = field(default_factory=list)
    # set only by a line-ranged read (see `_slice_lines`)
    partial: bool = False
    lines: Optional[List[List[int]]] = None    # contiguous runs actually returned
    total_lines: Optional[int] = None

    def to_dict(self, *, stringify_dates: bool = False) -> dict:
        """Serialize for an API layer. ``stringify_dates`` str()-coerces the
        timestamps (the MCP layer needs plain strings; FastAPI JSON-encodes
        datetimes itself, so the HTTP layer passes them through raw)."""
        def d(v):
            return (str(v) if v is not None else None) if stringify_dates else v
        return {"id": self.id, "content_hash": self.content_hash, "body": self.body,
                "title": self.title, "tags": self.tags, "path": self.path,
                "seq": self.seq, "created_at": d(self.created_at),
                "updated_at": d(self.updated_at), "expires_at": d(self.expires_at),
                "created": self.created, "moved_from": self.moved_from,
                "warnings": self.warnings, "partial": self.partial,
                "lines": self.lines, "total_lines": self.total_lines}


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


# The recipe new history rows are hashed with. Stored per row (`hash_version`)
# so rows written by older versions keep verifying against the recipe that
# produced them — see migrations/0013 for why the recipe changed.
HASH_VERSION = 2


def _dim_title(d):
    if d.get("title_before") is not None or d.get("title_after") is not None:
        return (d.get("title_before") or "", d.get("title_after") or "")
    return None


def _dim_author(d):
    if d.get("author_user_id"):
        return (d["author_user_id"], d.get("author_token_id") or "")
    return None


def _dim_valid_at(d):
    v = d.get("valid_at")
    return None if v is None else (v.isoformat() if hasattr(v, "isoformat")
                                   else str(v),)


# The optional dimensions of a history row, in the order they fold.
#
# ORDER IS PART OF THE RECIPE and entries are APPEND-ONLY: compute and verify walk
# this same tuple, so reordering or inserting in the middle changes the digest of
# rows already written and turns an untouched chain into a "tampered" one. A
# registry rather than a chain of `if`s because that requirement is a property of
# the list, and a list can be asserted in a test — an implicit ordering buried in
# statement order cannot.
#
# Each extractor returns the fields to fold, or None to fold NOTHING. That is what
# makes a dimension additive: a row that never used it hashes exactly as it did
# before the dimension existed, which is why adding one needs no version bump.
# The corollary matters as much: a build that does not know a dimension will
# mis-verify a row that USES it, so a new dimension must ship where no released
# client can meet such a row (see the v17 note in schema.py).
OPTIONAL_DIMENSIONS = (
    ("memgres.title.v1", _dim_title),
    ("memgres.author.v1", _dim_author),
    ("memgres.valid_at.v1", _dim_valid_at),
)


def _row_hash(prev: Optional[str], memory_id: str, seq: int, op: str,
              diff: Optional[str], hash_after: Optional[str],
              path_after: Optional[str], tags_after: Optional[Sequence[str]],
              source: Optional[str], reason: Optional[str],
              author_user_id: Optional[str] = None,
              author_token_id: Optional[str] = None,
              title_before: Optional[str] = None,
              title_after: Optional[str] = None,
              valid_at: object = None,
              version: int = HASH_VERSION) -> str:
    """The chain digest for one history row, computed by recipe ``version``.

    v1 left the tag list joined by commas inside the flat field list, which is
    not injective: ``['a','b']`` and ``['a,b']`` produce the same digest, so a
    tag set could be rewritten into another with the same joined text without
    breaking the chain. v2 folds tags the way title and author are already
    folded — each value reduced to a fixed-width digest before joining.

    v1 is kept, exactly as it was, because it is the only thing that can verify
    rows written before the change: rehashing stored history to "upgrade" it
    would rewrite the record whose immutability is the entire point.
    """
    tags = list(tags_after or [])
    flat_tags = ",".join(tags) if version < 2 else ""
    parts = [prev or "", memory_id, str(seq), op, diff or "", hash_after or "",
             path_after or "", flat_tags, source or "", reason or ""]
    h = _sha("\x1f".join(parts))
    # v2: the tags leave the flat list and fold in through their own domain. The
    # fold is unconditional (an empty tag set folds too), so a v2 row recomputed
    # under v1 — or a v1 row under v2 — cannot come out the same. That is what
    # keeps `hash_version` a selector rather than a claim the recipe must trust.
    if version == 2:
        h = _fold(h, "memgres.tags.v2", *tags)
    elif version != 1:
        # Any version this build has no branch for — a newer one, or a v3 whose
        # implementation someone forgot to add here — is not the same thing as a
        # broken chain and must not be reported as one. Note the test is "not a
        # version I implement", not "greater than the newest I know": the latter
        # would silently hash a forgotten version as if it were v1.
        raise ValueError(
            f"history row uses hash recipe v{version}; this build implements "
            f"v1 and v2 — a newer memgres wrote this row")
    # Each optional dimension folds in ONLY when it was touched on this row. A row
    # that touched none of them — which includes EVERY row written before those
    # features — returns the base digest unchanged and still verifies.
    dims = {"title_before": title_before, "title_after": title_after,
            "author_user_id": author_user_id, "author_token_id": author_token_id,
            "valid_at": valid_at}
    for label, extract in OPTIONAL_DIMENSIONS:
        fields = extract(dims)
        if fields is not None:
            h = _fold(h, label, *fields)
    return h


def _slice_lines(m: "Memory", spec: str) -> "Memory":
    """Return ``m`` carrying only the requested lines of its body.

    The slice is marked in three ways because a partial body that looks whole is
    the expensive mistake here: send it back as a new `body` and everything
    outside the slice is gone. `content_hash` is dropped rather than kept,
    since a hash that describes text the caller cannot see is worse than none.
    """
    body = m.body or ""
    all_lines = body.splitlines(keepends=True)
    wanted = parse_line_spec(spec, len(all_lines))
    if not wanted:
        # An empty body with `partial: true` is an answer-shaped nothing. The
        # caller asked for lines this memory does not have; say that.
        raise ValueError(
            f"no such lines: '{spec}' selects nothing in a {len(all_lines)}-line "
            f"memory")
    m.body = "".join(all_lines[i - 1] for i in wanted)
    m.content_hash = None
    m.partial = True
    # Contiguous RUNS, not first-and-last: `lines=1,5` returning [1, 5] reads as
    # "lines 1 through 5" and the caller believes it holds five.
    runs = []
    for n in wanted:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    m.lines = runs
    m.total_lines = len(all_lines)
    return m


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

    def _authorize_read(self, token: Optional[str], *, space=None, space_id=None):
        """Resolve a READ address that may span several namespaces.

        Returns ``(namespace_ids, names)`` — the ids every search must filter on,
        and ``{id: name}`` so a hit can say where it came from in the words the
        caller used. The single-namespace ``_authorize`` stays the entry point for
        writes and id-addressed reads; this one exists only for the search paths,
        which are the only ones that can legitimately span namespaces."""
        if not self._identity_on:
            return [""], {}
        token = token or self.cfg.default_token or None
        with self._conn.transaction():
            principal = identity.resolve(self._conn, self.cfg, token)
            resolved = identity.resolve_spaces(self._conn, principal,
                                               space=space, space_id=space_id)
        for nsid, perm in resolved:
            if not identity.perm_at_least(perm, "read"):
                raise identity.AuthError(
                    f"read permission required for namespace {nsid} "
                    f"(token grants {perm})")
        ns_ids = [nsid for nsid, _ in resolved]
        return ns_ids, self._space_names(ns_ids)

    @staticmethod
    def _clamp_k(k: int) -> int:
        """Bound how many hits one search may ask for.

        `k` went straight into `LIMIT`, so a single call could ask for the whole
        namespace — and with `full_body=true` be answered with it, plus a
        `ts_headline` over every row. The browse path has always clamped; search
        is the one an untrusted caller reaches most easily, so it clamps to the
        same ceiling."""
        return max(1, min(int(k), MAX_RESULTS))

    def _space_names(self, ns_ids: Sequence[str]) -> dict:
        """``{namespace_id: name}`` for the namespaces in play — one small query,
        so every hit can carry a human-readable space without joining per row."""
        if not ns_ids:
            return {}
        cur = self._conn.cursor()
        cur.execute("SELECT id, name FROM namespace WHERE id = ANY(%s)",
                    (list(ns_ids),))
        return {str(r[0]): r[1] for r in cur.fetchall()}

    def _expiry_sql(self) -> str:
        """The ``expires_at`` every write stamps, from the DEPLOYMENT's retention
        policy alone.

        There is deliberately no per-write override. Retention is the operator's
        promise about how long a client's data is kept, so a caller must not be
        able to lengthen it — and the `ttl_days` argument this replaced could, in
        three separate ways: `0` was read as "keep forever", a large value simply
        outran the policy, and an edit that merely omitted it silently cleared an
        expiry already set. Deriving the value from config alone makes all three
        impossible by construction rather than by validation.

        `retention_days <= 0` means the deployment keeps everything; that is the
        operator's choice too, and it is the default."""
        days = self.cfg.retention_days
        return "NULL" if days <= 0 else f"now() + interval '{int(days)} days'"

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
              at: Optional[str] = None, if_moved: str = "error",
              body: Optional[str] = None, diff: Optional[str] = None,
              base_hash: Optional[str] = None, path: Optional[str] = None,
              tags: Optional[Sequence[str]] = None, source: Optional[str] = None,
              reason: Optional[str] = None,
              title: Optional[str] = None, valid_at: object = None,
              replace: Optional[Sequence[str]] = None, replace_all: bool = False,
              space: Optional[str] = None, space_id: Optional[str] = None) -> Memory:
        """Create a memory, or edit one addressed by `id` or by `at` (its path).

        `at` and `path` are different jobs and must not be confused: `at` FINDS
        the memory to edit, `path` SETS where a memory lives. So `at='ops.x'`
        edits whatever is at ops.x, while `path='ops.x'` files a new memory there
        (or, alongside `at`, moves one). Keeping them apart is what lets a write
        by address never risk overwriting a memory the caller didn't mean to
        touch, without any flag saying which it meant.

        `if_moved` governs an address that only exists in history — see
        :meth:`_at_id`. Writes default to `'error'` rather than resolving
        silently: a caller writing to a stale address is working from a stale
        picture, and both quiet answers (edit the moved memory, or make a second
        one at the vacated path) commit them to it.
        """
        if replace is not None and id is None and at is None:
            raise ValueError("replace edits an existing memory — pass its id or `at`")
        if if_moved not in ("error", "follow", "create"):
            raise ValueError("if_moved must be 'error', 'follow' or 'create'")
        with self._conn.transaction():
            # authorize inside the tx so a lazily-created user/namespace commits
            # atomically with the write (or rolls back together on failure).
            ns, author = self._authorize(token, space=space, space_id=space_id,
                                         need="write", for_write=True)
            self._check_provenance_size(source, reason)
            self._check_title_size(title)
            # One spelling per tag, decided here rather than in `_create` and
            # `_update` separately — two normalisation sites is how a tag ends up
            # stored one way and filtered another.
            tags = normalize_tags(tags)
            valid_at = _as_date(valid_at)
            if id is None and at is None:
                self._check_path_free(ns, path, if_moved)
                m = self._create(ns, author, body, path, tags, source, reason,
                                 title, valid_at)
            else:
                id, moved_from = self._address(ns, id, at,
                                               follow=if_moved == "follow")
                m = self._update(ns, author, id, body, diff, base_hash, path,
                                 tags, source, reason, title=title,
                                 valid_at=valid_at,
                                 replace=replace, replace_all=replace_all)
                m.moved_from = moved_from
            # Checked on the STORED body, not the request: a substring edit or a
            # diff can introduce the stray tag just as a whole body can.
            m.warnings = write_warnings(m.body)
            return m

    def _check_path_free(self, ns: str, path: Optional[str],
                         if_moved: str) -> None:
        """Refuse to create at an address that is taken, or that was vacated.

        Taken is the easy half: the unique index refused it anyway, this just
        says so in words that name the occupant. Vacated is the half that
        matters. A caller creating at a path some memory moved away from is
        almost always working from an outdated address and means to edit that
        memory — and letting the create through gives them a SECOND memory on the
        same subject while the first goes on living elsewhere. Nothing errors,
        both turn up in recall, and the caller keeps writing to the wrong one.

        So it is refused, and the refusal says where the memory went, which is
        the fact that turns a blind retry into a decision. Genuinely reclaiming a
        vacated name is `if_moved='create'` — a deliberate second call, made by
        someone who has just been told what used to be there.
        """
        if not path:
            return
        cur = self._conn.cursor()
        cur.execute("SELECT id FROM memory WHERE namespace=%s AND path=%s::ltree",
                    (ns, path))
        row = cur.fetchone()
        if row is not None:
            raise PathTaken(path, str(row[0]))
        if if_moved == "create":
            return
        cur.execute(
            "SELECT h.memory_id, m.path::text FROM memory_history h "
            "JOIN memory m ON m.id = h.memory_id AND m.namespace = %s "
            "WHERE h.op = 'move' AND h.path_before = %s "
            "ORDER BY h.id DESC LIMIT 1",
            (ns, path))
        moved = cur.fetchone()
        if moved is not None:
            mid, now_at = str(moved[0]), moved[1]
            raise PathMoved(
                path, mid, now_at,
                f"'{path}' is not free: the memory that was there moved to "
                f"'{now_at}'. Edit it with at='{now_at}', or pass "
                f"if_moved='create' to claim the old path for something new")

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

    def _require_title(self, title: Optional[str], path: Optional[str],
                       body: Optional[str]) -> None:
        """Refuse content with no caption, naming enough of the memory that the
        caller can write one without re-reading it.

        Checked only where content is STORED — a create, or an edit that changes
        the body. Re-addressing (`move`) and relabelling (`retag`) leave the
        content alone, and demanding a caption there would make an untitled
        memory unmovable, which is friction unrelated to the point."""
        if not self.cfg.require_title or (title or "").strip():
            return
        first = ((body or "").strip().splitlines() or [""])[0][:120]
        where = f" at '{path}'" if path else ""
        raise MissingTitle(
            f"this memory{where} has no title — give `title` a short caption "
            f"(it is what names the memory in results and what title search "
            f"matches). Its first line is: {first!r}")

    def _check_title_size(self, title: Optional[str]):
        if title is not None and byte_len(title) > self.cfg.max_title_bytes:
            raise TooLarge(
                f"title is {byte_len(title)}B > MEMGRES_MAX_TITLE_BYTES "
                f"{self.cfg.max_title_bytes}")

    def _create(self, ns, author, body, path, tags, source, reason,
                title=None, valid_at=None) -> Memory:
        self._require_title(title, path, body)
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
                        1, %s, {self._expiry_sql()})
                RETURNING id, created_at, updated_at, expires_at""",
            [ns, body, chash, tags, path, self.cfg.fts_language, body,
             title, self.cfg.fts_language, title, pending],
        )
        mid, created_at, updated_at, expires_at = cur.fetchone()
        if pending:
            self._index_now(str(mid), body, ns, chash)   # inline unless async
        # store create as a diff-from-empty so the whole history is a self-contained
        # chain (empty → current), replayable forward for reconstruct/annotate. A
        # non-empty title at creation is audited (title_before None → title_after).
        self._sync_links(cur, ns, str(mid), body)
        self._bind_pending_links(cur, ns, str(mid), path)
        self._append_history(str(mid), 1, "create", make_diff("", body), None, chash,
                             None, path, None, tags, source, reason, author,
                             title_after=(title or None), valid_at=valid_at)
        return Memory(str(mid), body, chash, tags, path, 1, created_at,
                      updated_at, expires_at, title, created=True)

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
                source, reason, *, title=None, valid_at=None,
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
            elif valid_at is not None:
                # Re-confirming a fact changes no content, but it IS an assertion
                # and belongs in the chain — otherwise the only way to record
                # "still true on this date" would be a fake edit to the body.
                op = "revalidate"
            else:
                # pure touch: renew TTL, no history row
                cur.execute(
                    f"UPDATE memory SET updated_at=now(), expires_at={self._expiry_sql()} "
                    "WHERE id=%s", (id,))
                touched = self.get(None, id, _ns=ns, renew=False)
                touched.created = False
                return touched

        if body_changed:
            self._require_title(new_title, new_path, new_body)
            self._check_body_size(new_body)

        if path_changed:
            self._check_parent(cur, ns, new_path)

        if path_changed:
            # Moving onto an occupied address surfaced as a raw unique-index
            # violation, which named neither the path nor its occupant — the
            # same blind error creating there used to give.
            cur.execute("SELECT id FROM memory "
                        "WHERE namespace=%s AND path=%s::ltree AND id <> %s",
                        (ns, new_path, id))
            taken = cur.fetchone()
            if taken is not None:
                raise PathTaken(new_path, str(taken[0]))

        # a path change cascades to the whole subtree (keep ltree consistent)
        if path_changed and cur_path is not None:
            self._cascade_move(cur, ns, cur_path, new_path, id, author,
                               source, reason)

        cur.execute(
            f"""UPDATE memory SET body=%s, content_hash=%s, tags=%s, path=%s::ltree,
                    fts=to_tsvector(%s::regconfig, %s),
                    title=%s, title_fts=to_tsvector(%s::regconfig, %s),
                    seq=seq+1, updated_at=now(),
                    embed_pending=(embed_pending OR %s),
                    expires_at={self._expiry_sql()}
                WHERE id=%s
                RETURNING seq, created_at, updated_at, expires_at""",
            [new_body, new_hash, new_tags, new_path,
             self.cfg.fts_language, new_body,
             new_title, self.cfg.fts_language, new_title,
             (body_changed and self._vectors is not None), id])
        new_seq, created_at, updated_at, expires_at = cur.fetchone()
        if body_changed:
            self._sync_links(cur, ns, str(id), new_body)
        if path_changed:
            # The memory's own edges are pinned by id and follow it; what needs
            # attention is edges that were WAITING for the path it just took.
            self._bind_pending_links(cur, ns, str(id), new_path)
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
                             title_after=new_title if title_changed else None,
                             valid_at=valid_at)
        return Memory(str(id), new_body, new_hash, new_tags, new_path, new_seq,
                      created_at, updated_at, expires_at, new_title, created=False)

    def _cascade_move(self, cur, ns, old_prefix: str, new_prefix: str,
                      exclude_id, author, source, reason) -> None:
        """Re-address every descendant when a node moves — and RECORD it.

        This used to be one bulk UPDATE with no history: a descendant's address
        changed with nothing in `history` or `blame` to say so, and afterwards
        nothing could say where its old path had gone. A subtree move is a real
        change to a real memory, so each descendant gets a real `move` row.

        The rewrite rule lives HERE, in Python, and the UPDATE stores what it is
        told — rather than the rule being written once in SQL for the update and
        again for the history rows, where the two could disagree about which
        address a node just left.
        """
        # FOR UPDATE: the moved node is already locked by `_load`, but its
        # descendants were read unlocked, so a concurrent edit to one of them
        # could bump `seq` between this SELECT and the history INSERT — colliding
        # with UNIQUE (memory_id, seq) and aborting the move. Locking the subtree
        # makes the two writers queue instead of one of them failing.
        cur.execute(
            "SELECT id, path::text, seq, content_hash FROM memory "
            "WHERE namespace=%s AND path <@ %s::ltree AND id <> %s ORDER BY path "
            "FOR UPDATE",
            (ns, old_prefix, exclude_id))
        rows = cur.fetchall()
        if not rows:
            return
        # `path <@ old_prefix` matches the prefix itself or `prefix.<rest>`, so
        # swapping the prefix is the whole rule.
        moves = [(str(mid), old, new_prefix + old[len(old_prefix):], seq + 1, chash)
                 for (mid, old, seq, chash) in rows]
        cur.executemany(
            "UPDATE memory SET path=%s::ltree, seq=%s, updated_at=now() WHERE id=%s",
            [(after, seq, mid) for (mid, _b, after, seq, _c) in moves])
        self._append_move_history(cur, moves, author, source, reason)

    def _append_move_history(self, cur, moves, author, source, reason) -> None:
        """One `move` row per descendant, each chained onto ITS OWN last row.

        The hash chain is per memory, so appending to many at once is still N
        independent chains — but the previous hashes are read in one query rather
        than one per node, and the rows go in as one batch. Bodies don't change on
        a move, hence hash_before == hash_after and no diff."""
        author_user_id, author_token_id = author or (None, None)
        ids = [m[0] for m in moves]
        cur.execute(
            "SELECT DISTINCT ON (memory_id) memory_id, row_hash FROM memory_history "
            "WHERE memory_id = ANY(%s) ORDER BY memory_id, seq DESC", (ids,))
        prev = {str(r[0]): r[1] for r in cur.fetchall()}
        params = []
        for (mid, before, after, seq, chash) in moves:
            prev_hash = prev.get(mid)
            rhash = _row_hash(prev_hash, mid, seq, "move", None, chash, after,
                              None, source, reason, author_user_id, author_token_id,
                              None, None)
            params.append((mid, seq, "move", None, chash, chash, before, after,
                           None, None, source, reason, author_user_id,
                           author_token_id, None, None, prev_hash, rhash,
                           HASH_VERSION))
        cur.executemany(
            """INSERT INTO memory_history (memory_id, seq, op, diff, hash_before,
                   hash_after, path_before, path_after, tags_before, tags_after,
                   source, reason, author_user_id, author_token_id,
                   title_before, title_after, prev_row_hash, row_hash,
                   hash_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s::ltree,%s::ltree,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            params)

    def _append_history(self, memory_id, seq, op, diff, hash_before, hash_after,
                        path_before, path_after, tags_before, tags_after,
                        source, reason, author=None, *,
                        title_before=None, title_after=None, valid_at=None):
        author_user_id, author_token_id = author or (None, None)
        cur = self._conn.cursor()
        cur.execute("SELECT row_hash FROM memory_history WHERE memory_id=%s "
                    "ORDER BY seq DESC LIMIT 1", (memory_id,))
        prev = cur.fetchone()
        prev_hash = prev[0] if prev else None
        rhash = _row_hash(prev_hash, memory_id, seq, op, diff, hash_after,
                          path_after, tags_after, source, reason,
                          author_user_id, author_token_id,
                          title_before, title_after, valid_at)
        cur.execute(
            """INSERT INTO memory_history (memory_id, seq, op, diff, hash_before,
                   hash_after, path_before, path_after, tags_before, tags_after,
                   source, reason, author_user_id, author_token_id,
                   title_before, title_after, prev_row_hash, row_hash,
                   hash_version, valid_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s::ltree,%s::ltree,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (memory_id, seq, op, diff, hash_before, hash_after, path_before,
             path_after, tags_before, tags_after, source, reason,
             author_user_id, author_token_id, title_before, title_after,
             prev_hash, rhash, HASH_VERSION, valid_at))

    # ─── recall: lexical / semantic / hybrid ────────────────────────────────
    def recall(self, token: Optional[str], query: str, *, k: int = 10,
               tags: Optional[Sequence[str]] = None,
               path_prefix: Optional[str] = None, mode: str = "auto",
               match: Optional[str] = None,
               snippet: Optional[bool] = None, full_body: Optional[bool] = None,
               bodies: bool = True, match_tags: Optional[str] = None,
               space=None, space_id=None):
        """Search bodies AND curated titles. ``space``/``space_id`` may name one
        namespace, several, or ``'all'`` — see :func:`identity.resolve_spaces`.

        ``bodies=False`` is the light "where is it" pass: ranked hits with
        id/path/title/tags and no text. It replaces the separate ``find`` tool,
        which searched titles and nothing else — two half-searches the caller had
        to choose between, one of which answered "nothing found" for every
        memory that had no caption."""
        from .search import recall as _recall
        k = self._clamp_k(k)
        ns, names = self._authorize_read(token, space=space, space_id=space_id)
        hits = _recall(self._conn, self.cfg, self.embedder, ns,
                       query, k=k, tags=tags, path_prefix=path_prefix, mode=mode,
                       match=match, backend=self._vectors,
                       snippet=snippet, full_body=full_body, bodies=bodies,
                       tags_match=check_tag_match(match_tags))
        for h in hits:
            h.space = names.get(h.namespace)
        return hits

    # ─── links: the graph between memories ──────────────────────────────────
    def _sync_links(self, cur, ns: str, memory_id: str,
                    body: Optional[str]) -> None:
        """Re-derive this memory's outgoing edges from its body.

        Resolution is scoped to the memory's OWN namespace: `[[ops.x]]` written by
        one tenant must never bind to another tenant's `ops.x`. Paths are not a
        global address space, and an edge is a read path like any other.

        Rewritten wholesale rather than diffed — the body is the source of truth
        for its own links, and reconciling insert/update/delete against it would
        be more code with more ways to drift."""
        links = parse_links(body)
        cur.execute("DELETE FROM memory_link WHERE src_id=%s", (memory_id,))
        if not links:
            return
        wanted = [l.raw_target for l in links if l.scheme is None]
        found: dict = {}
        if wanted:
            cur.execute(
                "SELECT path::text, id FROM memory "
                "WHERE namespace=%s AND path = ANY(%s::ltree[])", (ns, wanted))
            found = {p: str(i) for p, i in cur.fetchall()}
        cur.executemany(
            "INSERT INTO memory_link (src_id, ord, dst_id, raw_target, label, "
            "anchor, scheme) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(memory_id, l.ord,
              found.get(l.raw_target) if l.scheme is None else None,
              l.raw_target, l.label, l.anchor, l.scheme) for l in links])

    def _bind_pending_links(self, cur, ns: str, memory_id: str,
                            path: Optional[str]) -> None:
        """Attach edges that were written pointing at `path` before anything lived
        there. A link to something not yet written is a deliberate marker, so it
        stays dangling until the target appears — and then it must actually bind,
        or the marker was a lie."""
        if not path:
            return
        cur.execute(
            "UPDATE memory_link l SET dst_id=%s "
            "FROM memory src "
            "WHERE src.id = l.src_id AND src.namespace = %s "
            "  AND l.dst_id IS NULL AND l.scheme IS NULL AND l.raw_target = %s",
            (memory_id, ns, path))

    def links(self, token: Optional[str], id: Optional[str] = None, *,
              at: Optional[str] = None, direction: str = "both",
              space=None, space_id=None) -> dict:
        """This memory's links: `out` (what it points at) and/or `in` (what points
        at it). Returns ``{"out": [...], "in": [...]}``.

        Inbound is the half a body cannot answer: "who relies on this?" is the
        question that matters when a fact changes, and reading the memory itself
        never tells you. Outbound is here too because it is where a DANGLING edge
        shows up — a link whose target was never written, or has since been
        erased."""
        if direction not in ("in", "out", "both"):
            raise ValueError(
                f"direction must be 'in', 'out' or 'both' (got {direction!r})")
        # One memory is addressed, so this authorizes like `get`/`history` (a
        # single namespace) rather than like `recall` (a set). That is also
        # exactly the right scope for the inbound half: an edge is only ever
        # bound within its source's namespace, so every backlink to this memory
        # lives in this namespace by construction.
        ns, _ = self._authorize(token, space=space, space_id=space_id,
                                need="read")
        mid, _moved = self._address(ns, id, at, follow=True)
        cur = self._conn.cursor()
        out: dict = {}
        if direction in ("out", "both"):
            cur.execute(
                "SELECT l.ord, l.raw_target, l.label, l.anchor, l.scheme, "
                "       l.dst_id::text, m.path::text, m.title "
                "FROM memory_link l LEFT JOIN memory m ON m.id = l.dst_id "
                "WHERE l.src_id = %s ORDER BY l.ord", (mid,))
            out["out"] = [
                {"target": r[1], "label": r[2], "anchor": r[3], "scheme": r[4],
                 "id": r[5], "path": r[6], "title": r[7],
                 "resolved": r[5] is not None}
                for r in cur.fetchall()]
        if direction in ("in", "both"):
            # The namespace predicate is re-applied rather than trusted to the
            # binding rule — a backlink names a memory, and naming one from a
            # namespace the caller cannot read would leak that it exists, where
            # it lives and what it is called. Same defence in depth as
            # `fetch_hit_rows` re-checking every vector candidate.
            cur.execute(
                "SELECT src.id::text, src.path::text, src.title, l.label, l.anchor "
                "FROM memory_link l JOIN memory src ON src.id = l.src_id "
                "WHERE l.dst_id = %s AND src.namespace = %s "
                "  AND (src.expires_at IS NULL OR src.expires_at > now()) "
                "ORDER BY src.path, src.id", (mid, ns))
            out["in"] = [{"id": r[0], "path": r[1], "title": r[2],
                          "label": r[3], "anchor": r[4]}
                         for r in cur.fetchall()]
        return out

    # ─── tags: the vocabulary actually in use ───────────────────────────────
    def tags(self, token: Optional[str], *, prefix: Optional[str] = None,
             k: int = 50, space=None, space_id=None) -> List[dict]:
        """The tags in use across the namespaces you reach, most-used first.

        A caller cannot reuse a label it has never seen. Without this, every
        writer invents its own spelling and the tag filter decays into a pile of
        single-use labels — which is exactly what happened here (265 distinct
        tags across 97 memories) before normalisation and this call existed."""
        from .tags import tag_counts
        ns, _ = self._authorize_read(token, space=space, space_id=space_id)
        return tag_counts(self._conn, ns, prefix=prefix,
                          k=max(1, min(int(k), MAX_RESULTS)))

    # ─── list: enumerate a subtree (no query, no ranking) ───────────────────
    def list(self, token: Optional[str], *, path_prefix: Optional[str] = None,
             tags: Optional[Sequence[str]] = None, limit: int = 50,
             offset: int = 0, bodies: bool = False,
             match_tags: Optional[str] = None,
             space=None, space_id=None) -> List[dict]:
        """Browse (enumerate) memories under a subtree — NOT a search: no FTS, no
        vectors, no scoring. Rows are ordered by path so a subtree reads in tree
        order, and across several namespaces by namespace first, so each tree
        still reads whole. ``build_filters`` scopes by namespace + not-expired +
        tags + subtree, so this is multi-tenant safe by construction.

        ``bodies=True`` returns whole bodies instead of previews — reading a
        subtree in one call rather than a browse plus one fetch per row. It is
        capped by ``MEMGRES_LIST_BODIES_MAX_BYTES`` in total: rows past the cap
        still come back, with ``body`` None and ``body_omitted`` True, so a
        truncated read announces itself instead of looking like a complete one.
        """
        from .vector.base import build_filters
        ns, names = self._authorize_read(token, space=space, space_id=space_id)
        limit = max(1, min(int(limit), MAX_RESULTS))
        offset = max(0, int(offset))
        where, params = build_filters(ns, tags, path_prefix,
                                      check_tag_match(match_tags))
        cur = self._conn.cursor()
        # The page itself never carries bodies: in `bodies` mode it carries each
        # body's SIZE instead. Deciding what fits from the sizes means the bodies
        # that don't fit are never transferred at all — the cap bounds the server
        # and the wire, not just the answer. (Selecting the bodies and discarding
        # them afterwards still moved every byte: 500 rows at the default body
        # ceiling is 128 MB fetched to return 200 KB.)
        shown = ("octet_length(body)" if bodies
                 else "left(split_part(body, E'\\n', 1), %s)")
        head = [] if bodies else [self.cfg.list_preview_chars]
        cur.execute(
            f"SELECT id, path::text, tags, title, {shown} AS shown, "
            "created_at, updated_at, namespace "
            f"FROM memory WHERE {where} ORDER BY namespace, path, id "
            "LIMIT %s OFFSET %s",
            head + params + [limit, offset])
        cols = ["id", "path", "tags", "title", "shown", "created_at",
                "updated_at", "space_id"]
        budget = self.cfg.list_bodies_max_bytes
        rows, wanted = [], []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["id"] = str(d["id"])
            d["tags"] = list(d["tags"]) if d["tags"] is not None else []
            d["space_id"] = str(d["space_id"])
            d["space"] = names.get(d["space_id"])
            shown_value = d.pop("shown")
            if not bodies:
                d["preview"] = shown_value
            else:
                # Once the cap is reached everything after it is omitted, rather
                # than letting a later short body slip through — a patchy result
                # is harder to reason about than a clean cutoff. The first row
                # always comes back whole even if it alone exceeds the cap: a
                # browse consisting only of omissions would tell you nothing.
                size = shown_value or 0
                fits = budget >= 0 and (not rows or size <= budget)
                budget = budget - size if fits else -1
                d["body"] = None
                d["body_omitted"] = not fits
                if fits:
                    wanted.append(d["id"])
            rows.append(d)
        if wanted:
            cur.execute(
                f"SELECT id, body FROM memory WHERE {where} AND id = ANY(%s)",
                params + [wanted])
            got = {str(i): b for i, b in cur.fetchall()}
            for d in rows:
                if not d["body_omitted"]:
                    d["body"] = got.get(d["id"])
        return rows

    # ─── addressing by path ─────────────────────────────────────────────────
    def _at_id(self, ns: str, at: str, *, follow: bool) -> tuple:
        """Resolve a path to ``(memory_id, moved_from)``.

        A path is unique within a namespace (``memory_ns_path_uniq``), so it is a
        real address — but a mutable one, which is the whole difficulty. Three
        outcomes, and the middle one is the reason this exists:

        * something lives there now → that memory, ``moved_from`` None;
        * nothing lives there, but something MOVED away → that memory, if
          ``follow``; otherwise :class:`PathMoved`, naming where it went;
        * nothing, and nothing ever moved away → :class:`NotFound`.

        The trail comes from ``memory_history``: a ``move`` row records the
        address a memory left. No redirect table, and no chain to walk — the row
        names the memory, and the memory's CURRENT path is authoritative however
        many times it has moved since. Deletion is real (history cascades with the
        row), so a deleted memory leaves no redirect and its path is simply free
        again: exactly the addresses where a fork is impossible.
        """
        cur = self._conn.cursor()
        cur.execute("SELECT id FROM memory WHERE namespace=%s AND path=%s::ltree",
                    (ns, at))
        row = cur.fetchone()
        if row is not None:
            return str(row[0]), None
        # A live path always wins over a redirect: a vacated address may since
        # have been claimed on purpose, and the thing that is there now is what
        # the caller reached for.
        cur.execute(
            "SELECT h.memory_id, m.path::text FROM memory_history h "
            "JOIN memory m ON m.id = h.memory_id AND m.namespace = %s "
            "WHERE h.op = 'move' AND h.path_before = %s "
            "ORDER BY h.id DESC LIMIT 1",     # the most recent departure wins
            (ns, at))
        moved = cur.fetchone()
        if moved is None:
            raise NotFound(f"no memory at path '{at}'")
        mid, now_at = str(moved[0]), moved[1]
        if follow:
            return mid, at
        raise PathMoved(
            at, mid, now_at,
            f"'{at}' moved to '{now_at}' — address it there, or pass "
            f"if_moved='follow' to edit it where it is now")

    def _address(self, ns: str, id: Optional[str], at: Optional[str], *,
                 follow: bool) -> tuple:
        """``(memory_id, moved_from)`` from whichever address the caller gave.

        One definition for every id-addressed operation, so `at` behaves the same
        on a read, an edit and a delete rather than being re-implemented per
        method."""
        if id is not None and at is not None:
            raise ValueError("pass either `id` or `at` (a path), not both")
        if id is not None:
            return id, None
        if at is None:
            raise ValueError("need `id` or `at` (a path) to address a memory")
        return self._at_id(ns, at, follow=follow)

    # ─── convenience: move ──────────────────────────────────────────────────
    def move(self, token: Optional[str], id: Optional[str] = None,
             new_path: Optional[str] = None, *, at: Optional[str] = None,
             if_moved: str = "error",
             source: Optional[str] = None, reason: Optional[str] = None,
             space: Optional[str] = None, space_id: Optional[str] = None) -> Memory:
        """Re-address a memory (and, by cascade, its subtree). Address the memory
        by `id` or by `at` (its current path); `new_path` is where it goes."""
        # `new_path` is only optional in the signature so `at=` can be passed by
        # keyword. A move with no destination is meaningless, and left to fall
        # through it would reach `write` as a metadata-free edit — a silent no-op.
        if not new_path:
            raise ValueError("move needs a destination path")
        return self.write(token, id=id, at=at, if_moved=if_moved, path=new_path,
                          source=source, reason=reason,
                          space=space, space_id=space_id)

    # ─── read ───────────────────────────────────────────────────────────────
    def get(self, token: Optional[str], id: Optional[str] = None, *,
            at: Optional[str] = None, if_moved: str = "follow",
            lines: Optional[str] = None,
            renew: bool = True, _ns: Optional[str] = None,
            space: Optional[str] = None, space_id: Optional[str] = None) -> Memory:
        """Fetch one memory by `id`, or by `at` — the path it lives at.

        A read follows a move by default: the memory that used to live at that
        path is what the caller reached for, and it comes back with `moved_from`
        set so they learn the address changed. Pass `if_moved='error'` to be told
        instead of redirected.

        `lines` ("40-80", "5", "1,10-12") returns only those lines of the body,
        for reading part of something long without paying for all of it. The
        result then says so loudly — `partial` is set, `lines` lists the
        contiguous runs actually returned, `total_lines` the whole size, and
        `content_hash` comes back **None**, because the one dangerous thing to do
        with a slice is send it back as a whole `body` and erase everything
        around it. A selection that matches nothing is an error, not an empty
        body."""
        ns = _ns if _ns is not None else self._authorize(
            token, space=space, space_id=space_id, need="read")[0]
        id, moved_from = self._address(ns, id, at, follow=if_moved == "follow")
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
                    f"UPDATE memory SET expires_at={self._expiry_sql()} WHERE id=%s",
                    (id,))
        m = Memory(str(row[0]), row[1], row[2], list(row[3]), row[4], row[5],
                   row[6], row[7], row[8], row[9], moved_from=moved_from)
        return _slice_lines(m, lines) if lines else m

    def history(self, token: Optional[str], id: Optional[str] = None, *,
                at: Optional[str] = None, if_moved: str = "follow",
                space: Optional[str] = None,
                space_id: Optional[str] = None) -> List[dict]:
        ns, _ = self._authorize(token, space=space, space_id=space_id, need="read")
        id, _moved = self._address(ns, id, at, follow=if_moved == "follow")
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
            "COALESCE(NULLIF(u.full_name, ''), NULLIF(u.name, '')) AS author_name, "
            "NULLIF(u.email, '') AS author_email, "
            "h.title_before, h.title_after, h.hash_version, h.valid_at, "
            "h.prev_row_hash, h.row_hash, h.created_at FROM memory_history h "
            "LEFT JOIN app_user u ON u.id = h.author_user_id "
            "WHERE h.memory_id=%s ORDER BY h.seq", (id,))
        cols = ["seq", "op", "diff", "hash_before", "hash_after", "path_before",
                "path_after", "tags_before", "tags_after", "source", "reason",
                "author_user_id", "author_token_id", "author_name", "author_email",
                "title_before", "title_after", "hash_version", "valid_at",
                "prev_row_hash", "row_hash", "created_at"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def annotate(self, token: Optional[str], id: Optional[str] = None,
                 upto_seq: Optional[int] = None,
                 lines: Optional[Sequence[int]] = None, *,
                 at: Optional[str] = None,
                 space: Optional[str] = None, space_id: Optional[str] = None) -> List[dict]:
        """Blame: the body with each line tagged by who last changed it. Pass
        `lines` (1-based line numbers) to return only those lines."""
        from .blame import annotate as _annotate
        return _annotate(self.history(token, id, at=at, space=space,
                                      space_id=space_id),
                         upto_seq, lines)

    def annotate_grouped(self, token: Optional[str], id: Optional[str] = None,
                         upto_seq: Optional[int] = None,
                         include_text: bool = True, *,
                         at: Optional[str] = None,
                         space: Optional[str] = None,
                         space_id: Optional[str] = None) -> List[dict]:
        """Blame as runs: consecutive same-author lines collapse into one block.
        `include_text=False` returns a pure ownership map (ranges, no body)."""
        from .blame import annotate_grouped as _grouped
        return _grouped(self.history(token, id, at=at, space=space,
                                     space_id=space_id),
                        upto_seq, include_text)

    def reconstruct(self, token: Optional[str], id: Optional[str] = None,
                    upto_seq: Optional[int] = None, *, at: Optional[str] = None,
                    space: Optional[str] = None, space_id: Optional[str] = None) -> str:
        """The exact body text at a past version (default current)."""
        from .blame import reconstruct as _reconstruct
        return _reconstruct(self.history(token, id, at=at, space=space,
                                         space_id=space_id),
                            upto_seq)

    def verify_history(self, token: Optional[str], id: Optional[str] = None, *,
                       at: Optional[str] = None,
                       space: Optional[str] = None,
                       space_id: Optional[str] = None) -> bool:
        """Recompute the chain; True if untampered.

        Each row is recomputed with the recipe it records (`hash_version`), so a
        chain written across an upgrade verifies end to end: the rows from before
        keep their original digests and the rows after use the stronger one.
        """
        # resolved here rather than inside `history`, because the memory id is
        # itself hashed into every row — verifying against the address the caller
        # typed instead of the id it resolves to would fail every chain.
        ns, _ = self._authorize(token, space=space, space_id=space_id, need="read")
        id, _moved = self._address(ns, id, at, follow=True)
        rows = self.history(token, id, space=space, space_id=space_id)
        prev = None
        for r in rows:
            expect = _row_hash(prev, id, r["seq"], r["op"], r["diff"],
                               r["hash_after"], r["path_after"], r["tags_after"],
                               r["source"], r["reason"],
                               r["author_user_id"], r["author_token_id"],
                               r["title_before"], r["title_after"],
                               r["valid_at"],
                               version=int(r["hash_version"] or 1))
            if expect != r["row_hash"] or (r["prev_row_hash"] or None) != prev:
                return False
            prev = r["row_hash"]
        return True

    # ─── forget: real erasure ───────────────────────────────────────────────
    def forget(self, token: Optional[str], id: Optional[str] = None, *,
               at: Optional[str] = None, if_moved: str = "error",
               space: Optional[str] = None,
               space_id: Optional[str] = None) -> bool:
        """Erase a memory addressed by `id` or by `at` (its path).

        Like every write, a stale address is refused rather than followed:
        deleting the memory that used to live somewhere else, on the strength of
        an address that no longer means what the caller thinks, is the one
        mistake here that cannot be undone."""
        ns, _ = self._authorize(token, space=space, space_id=space_id, need="write")
        id, _moved = self._address(ns, id, at, follow=if_moved == "follow")
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
        """Delete the memories whose retention window has closed; returns how many.

        ``build_filters`` already hides expired rows from every read, so this is
        not what makes them invisible — it is what makes them GONE, and the two
        are not interchangeable. A retention promise is about no longer HOLDING
        the data; a row that is merely filtered out of results is still held.

        Chunk vectors go with them, exactly as in :meth:`forget`. pgvector
        cascades on the foreign key, but qdrant is an out-of-band collection:
        skip this and its points outlive the memory, keep taking candidate slots
        in every search, and are then dropped when ``fetch_hit_rows`` finds no
        row behind them — recall quietly thinning out rather than an error."""
        with self._conn.transaction():
            cur = self._conn.cursor()
            cur.execute("DELETE FROM memory WHERE expires_at IS NOT NULL "
                        "AND expires_at < now() RETURNING id, namespace")
            gone = [(str(r[0]), r[1]) for r in cur.fetchall()]
        if gone and self._vectors is not None:
            for mid, ns in gone:
                self._vectors.delete_chunks(self._conn, mid, ns)
        return len(gone)
