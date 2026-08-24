"""Identity, tenancy & tokens — payment-agnostic primitives.

memgres knows **nothing** about wallets or payments. It knows users, namespaces,
tokens, memberships and access-requests. A payment/gateway layer is built on top
and is just an ordinary :class:`app_user` that routes.

Two orthogonal axes (never conflate them):

* **organize** memories *within* a space → the tree (`ltree` path) + tags;
* **isolate** between accounts → the **namespace**.

Addressing a space is **id-canonical**: ``namespace.id`` is the unambiguous
address (memberships, ``memory.namespace`` all use it). A *name* is a
convenience that resolves against every namespace the caller can reach — their
own and any shared with them — plus their own aliases. Since names are unique
only per owner, two reachable spaces can carry one name; that is refused when
addressed, and settled with an alias. See ``resolve_space`` and
``_resolve_name``.

This module is pure DB logic over a psycopg connection — no HTTP, no MCP. The
:class:`~memgres.store.Store` (v0.2, phase 3) calls into it to authenticate and
resolve a space before every operation.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# ─── token format: mgk_ + 43 url-safe chars (256-bit) ────────────────────────
TOKEN_RE = re.compile(r"^mgk_[A-Za-z0-9_-]{43}$")


def bearer_token(authorization: Optional[str],
                 x_memgres_token: Optional[str]) -> Optional[str]:
    """Extract a raw memgres token from request auth headers — ``Authorization:
    Bearer <tok>`` first, else the ``X-Memgres-Token`` header — or ``None`` if
    neither is present. One definition for both the HTTP and MCP transports so
    header parsing can't drift between them."""
    if authorization and authorization[:7].lower() == "bearer ":
        return authorization[7:].strip() or None
    if x_memgres_token:
        return x_memgres_token.strip() or None
    return None

# permission lattice
_RANK = {"read": 1, "write": 2, "admin": 3}

# service roles (app_user.role) — orthogonal to the per-namespace permission
# lattice above. `user` is the default; the two admin roles govern the CONTROL
# plane (provisioning) and, for superadmin, cross-namespace data access:
#   user         — owns namespaces, manages access to its OWN spaces only.
#   user_manager — user + create users + (re)issue tokens. No cross-tenant data.
#   superadmin   — full root: read/write any namespace, grant any access,
#                  grant/revoke roles. Principal.is_admin derives from this.
SERVICE_ROLES = ("user", "user_manager", "superadmin")
ADMIN_ROLES = ("user_manager", "superadmin")   # roles that carry any authority


class AuthError(PermissionError):
    """Bad/expired/revoked token, or the token may not do this here."""


class SpaceNotFound(KeyError):
    """No namespace the caller can reach for the given address."""


class SpaceAmbiguous(ValueError):
    """The caller reaches several namespaces and named none of them.

    Deliberately not a `SpaceNotFound`: that maps to "no such thing", and this
    is the opposite — there are too many, so the request is under-specified and
    the caller must choose. Being a ValueError it already surfaces as a 422 and
    carries the candidates in its message, which is the only useful answer.
    """


def new_token() -> str:
    """A fresh server-generated secret: ``mgk_`` + 43 url-safe chars."""
    return "mgk_" + secrets.token_urlsafe(32)


def token_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def valid_format(secret: str) -> bool:
    return bool(secret) and bool(TOKEN_RE.match(secret))


def perm_at_least(have: str, need: str) -> bool:
    return _RANK[have] >= _RANK[need]


def perm_min(a: str, b: str) -> str:
    """The weaker of two permissions (a token ceiling min a membership)."""
    return a if _RANK[a] <= _RANK[b] else b


@dataclass
class Principal:
    """Who a token authenticates as, plus its ceiling and scope.

    ``user_id`` is None for the global admin, and None-but-provisional for a
    valid open-mode token whose user row is created lazily on the first write.
    """
    user_id: Optional[str]
    permission: str                        # token ceiling: read|write|admin
    scope_namespace_id: Optional[str]      # scoped to one ns, or None = all the user's
    token_id: Optional[str] = None
    token_hash: Optional[str] = None
    is_admin: bool = False                 # full root: env break-glass (user_id
                                           # None) or a superadmin-role user
    provisional: bool = False              # valid token, user not yet materialized
    role: str = "user"                     # service role of the owning user


def can_manage_users(p: "Principal") -> bool:
    """May this principal provision users / (re)issue tokens? True for a
    user_manager, a superadmin, or the env break-glass root."""
    return p.is_admin or p.role in ADMIN_ROLES


def can_create_namespace(conn, p: "Principal") -> bool:
    """May this principal bring a new namespace into existence?

    An admin role always may — it provisions for others by definition. Everyone
    else carries the right individually, so an ordinary member can be trusted to
    organize their own corner without also being able to provision people.
    """
    if can_manage_users(p):
        return True
    if p.user_id is None:                    # provisional: nothing to consult yet
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT can_create_namespace FROM app_user WHERE id=%s",
                    (p.user_id,))
        row = cur.fetchone()
    return bool(row and row[0])


# ─── authentication ──────────────────────────────────────────────────────────
def resolve(conn, cfg, secret: Optional[str], *, touch: bool = True) -> Principal:
    """Authenticate a bearer secret into a :class:`Principal`.

    * known token → its user/ceiling/scope/role (rejects revoked/expired). A
      token whose user is a superadmin resolves with ``is_admin`` — so once
      bootstrap has stored the env token as a real superadmin's token, that same
      secret authenticates as the *attributed* user, not the anonymous root.
    * env ``admin_token`` (break-glass) → anonymous admin principal. Tried only
      *after* the stored-token lookup, so a seeded env token attributes to its
      user; reachable before the first seed, or in modes bootstrap skips.
    * unknown but well-formed token in ``open`` mode → *provisional* principal
      (user materialized on first write); in ``managed`` mode → rejected.

    ``touch=False`` skips stamping ``last_used_at``. It is for callers that are
    not acting on the credential's behalf but merely asking about it — the MCP
    tool-list filter runs on every ``tools/list``, and counting that as "used"
    would both turn a listing into a write transaction and make the column mean
    "last connected" instead of "last did something".
    """
    if not secret:
        raise AuthError("a token is required")

    h = token_hash(secret)
    if valid_format(secret):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.user_id, t.namespace_id, t.permission, "
                "       (t.revoked_at IS NOT NULL) AS revoked, "
                "       (t.expires_at IS NOT NULL AND t.expires_at <= now()) AS expired, "
                "       u.role "
                "FROM token t JOIN app_user u ON u.id = t.user_id "
                "WHERE t.token_hash=%s", (h,))
            row = cur.fetchone()
            if row is not None:
                tid, uid, nsid, perm, revoked, expired, role = row
                if revoked:
                    raise AuthError("token revoked")
                if expired:
                    raise AuthError("token expired")
                if touch:
                    cur.execute("UPDATE token SET last_used_at=now() WHERE id=%s",
                                (tid,))
                return Principal(user_id=str(uid), permission=perm,
                                 scope_namespace_id=str(nsid) if nsid else None,
                                 token_id=str(tid), token_hash=h, role=role,
                                 is_admin=(role == "superadmin"))

    # env break-glass root — any format (an operator may set a non-mgk secret);
    # constant-time compare. Never reached for a seeded env token (matched above).
    if cfg.admin_token and hmac.compare_digest(secret, cfg.admin_token):
        return Principal(user_id=None, permission="admin",
                         scope_namespace_id=None, is_admin=True)

    if not valid_format(secret):
        raise AuthError("malformed token (expected mgk_ + 43 url-safe chars)")
    if cfg.key_mode == "open":
        # accepted, but nothing is created until the first write
        return Principal(user_id=None, permission="write",
                         scope_namespace_id=None, token_hash=h, provisional=True)
    raise AuthError("unknown token")


def ensure_user_for_token(conn, principal: Principal) -> str:
    """Materialize the user + persist the token row for a provisional principal
    (open mode, first write). Mutates ``principal`` in place and returns its id."""
    if principal.user_id is not None:
        return principal.user_id
    if not principal.token_hash:
        raise AuthError("cannot materialize a user without a token")
    with conn.cursor() as cur:
        # Open mode is self-service: nobody provisions these accounts, so there
        # is no admin to ask for the right to make a namespace. Withholding it
        # here would leave a materialized user unable to write anywhere.
        cur.execute("INSERT INTO app_user (can_create_namespace) VALUES (true) "
                    "RETURNING id")
        uid = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO token (token_hash, user_id, permission) "
            "VALUES (%s, %s, 'write') ON CONFLICT (token_hash) DO NOTHING "
            "RETURNING id", (principal.token_hash, uid))
        r = cur.fetchone()
        if r is None:  # lost a race: the token now exists, adopt its user
            cur.execute("SELECT id, user_id, permission, namespace_id "
                        "FROM token WHERE token_hash=%s", (principal.token_hash,))
            tid, existing_uid, perm, nsid = cur.fetchone()
            cur.execute("DELETE FROM app_user WHERE id=%s", (uid,))
            principal.user_id = str(existing_uid)
            principal.token_id = str(tid)
            principal.permission = perm
            principal.scope_namespace_id = str(nsid) if nsid else None
        else:
            principal.token_id = str(r[0])
            principal.user_id = uid
    principal.provisional = False
    return principal.user_id


# ─── space resolution (id-canonical; a name is a convenience, and may be an
#     alias — see _resolve_name) ─────────────────────────────────────────────
def _reach(cur, user_id: str, namespace_id: str) -> Optional[str]:
    """The caller's effective permission on a namespace, or None if unreachable.

    One query rather than two, and not only to save a round trip: the two-query
    form did strictly less work when the namespace did not exist than when it
    existed but was unreachable, and `request_access` — whose whole point is
    that those two cases must be indistinguishable — runs through here. Making
    the answers identical while the work differs just moves an oracle into the
    clock.
    """
    cur.execute(
        "SELECT CASE WHEN n.owner_user_id = %(u)s THEN 'admin' "
        "            ELSE m.permission END "
        "FROM namespace n "
        "LEFT JOIN namespace_member m "
        "       ON m.namespace_id = n.id AND m.user_id = %(u)s "
        "WHERE n.id = %(ns)s",
        {"u": user_id, "ns": namespace_id})
    row = cur.fetchone()
    return row[0] if row else None


def _resolve_name(cur, uid: str, name: str) -> str:
    """A name to a namespace id, or an error explaining which name it was.

    Names come from two places and are checked in that order:

    * **your alias** — a label you set yourself;
    * **any namespace you reach that carries this name** — your own, or one
      shared with you.

    The alias wins, and only ever against the second kind. Every collision you
    could cause yourself is refused when you create it (see ``create_alias``),
    so the one that survives is the one someone ELSE causes by sharing a
    namespace whose name matches your alias — and there your own deliberate
    label should not be broken by a stranger's act.

    Two shared namespaces of the same name are genuinely ambiguous: nobody chose
    that, so it is refused with both candidates named, and you settle it by
    giving them aliases.
    """
    cur.execute("SELECT namespace_id FROM namespace_alias "
                "WHERE user_id=%s AND alias=%s", (uid, name))
    row = cur.fetchone()
    if row is not None:
        return str(row[0])

    cur.execute(
        "SELECT n.id, NULLIF(u.name, '') FROM namespace n "
        "JOIN app_user u ON u.id = n.owner_user_id "
        "LEFT JOIN namespace_member m ON m.namespace_id=n.id AND m.user_id=%(u)s "
        "WHERE n.name=%(name)s AND (n.owner_user_id=%(u)s OR m.user_id=%(u)s)",
        {"u": uid, "name": name})
    rows = cur.fetchall()
    if len(rows) == 1:
        return str(rows[0][0])
    if not rows:
        raise SpaceNotFound(
            f"you can reach no namespace named '{name}' — create one, ask for "
            f"access to it, or address it by `space_id`")
    owners = ", ".join(f"{nid} (owner: {owner or 'unnamed'})" for nid, owner in rows)
    raise SpaceAmbiguous(
        f"'{name}' means {len(rows)} different namespaces you can reach: "
        f"{owners}. Give them aliases and use those, or address one by `space_id`")


def resolve_space(conn, principal: Principal, *, space_id: Optional[str] = None,
                  space: Optional[str] = None,
                  for_write: bool = False) -> Tuple[str, str]:
    """Resolve an address to ``(namespace_id, effective_permission)``.

    * ``space_id`` — canonical; any reachable space (owned or shared).
    * ``space`` — a name: your alias, or a namespace you reach that carries it
      (see :func:`_resolve_name`).
    * neither — your token's scope, or your single reachable namespace. Several
      reachable and none named is an error.

    **Nothing is created here.** Addressing a namespace that does not exist used
    to bring one into being — a name typed slightly wrong produced a new, empty,
    plausible-looking space and the write landed in it. Creating a namespace is
    now something you ask for (``create_own_namespace``), so a typo is an error
    instead of a place. ``for_write`` still materializes an open-mode user on
    first write; that is a user row, not a namespace.

    The returned permission is the token ceiling min the caller's membership; the
    caller enforces it against the op needed.
    """
    if principal.is_admin and principal.user_id is None:
        # env break-glass root: anonymous, addresses only by id
        if space_id:
            return str(space_id), "admin"
        raise AuthError("global admin must address a space by id")

    if principal.user_id is None:            # provisional (open mode)
        if for_write:
            ensure_user_for_token(conn, principal)
        else:
            raise SpaceNotFound("token has no namespaces yet")
    uid = principal.user_id
    scope = principal.scope_namespace_id
    ceiling = principal.permission

    def _scoped_ok(nsid: str):
        if scope is not None and str(nsid) != scope:
            raise AuthError("token is scoped to a different namespace")

    with conn.cursor() as cur:
        # 1) by id — reach anything owned or shared; a superadmin user reaches
        #    ANY space by id (full root), still capped by its token ceiling.
        if space_id is not None:
            _scoped_ok(space_id)
            perm = _reach(cur, uid, str(space_id))
            if perm is None:
                if principal.is_admin:
                    return str(space_id), perm_min("admin", ceiling)
                raise SpaceNotFound(f"namespace {space_id} not reachable")
            return str(space_id), perm_min(perm, ceiling)

        # 2) by name — your alias, else any reachable namespace of that name
        if space is not None:
            nsid = _resolve_name(cur, uid, space)
            _scoped_ok(nsid)
            perm = _reach(cur, uid, nsid)
            if perm is None:                      # alias to something since lost
                raise SpaceNotFound(f"'{space}' is no longer reachable")
            return nsid, perm_min(perm, ceiling)

        # 3) nothing named. A scoped token has already said where it works; one
        #    reachable namespace leaves nothing to choose. Beyond that, silence
        #    is a guess, and a guess about where data goes is the expensive kind.
        if scope is not None:
            perm = _reach(cur, uid, scope)
            if perm is None:
                raise SpaceNotFound(f"token scope {scope} not reachable")
            return scope, perm_min(perm, ceiling)

    reachable = list_spaces(conn, uid)
    if len(reachable) == 1:
        only = reachable[0]
        return only["id"], perm_min(only["permission"], ceiling)
    if len(reachable) > 1:
        names = ", ".join(sorted(s["name"] for s in reachable))
        raise SpaceAmbiguous(
            f"you can reach {len(reachable)} namespaces ({names}) — name the "
            "one you mean with `space`")
    raise SpaceNotFound(
        "you can reach no namespaces yet — create one, or ask for access to one")


# The two words that address a SET of namespaces rather than one.
#
# `all` means every namespace you reach as a member. For a superadmin that is
# NOT everything it can read — its role reaches any namespace by id — so for
# that one caller the word asks two different questions, and answering the
# narrow one silently is a partial result wearing the shape of a complete one.
# `*` is the wide answer, said out loud.
#
# There is deliberately no second word for "the ones I belong to": a namespace
# can be called anything, and the obvious candidates (`mine`, `own`) are names
# people actually use — the first draft of this shadowed a namespace literally
# named `mine` in this repo's own tests. `*` survives that objection because a
# namespace named `*` is not something anyone types by accident, and the
# collision is still checked rather than assumed away.
ALL_SPACES = "all"
EVERY_SPACE = "*"


def _as_list(v) -> List[str]:
    """One address or several. A bare str is ONE name/id, never a char sequence."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _wants(space, keyword: str) -> bool:
    """True when ``space`` IS this keyword — as a bare string or as the sole
    element of a list. Anywhere else (``["all", "notes"]``) it is read as a
    literal namespace name, so a keyword can never silently widen a list the
    caller meant literally."""
    names = _as_list(space)
    return len(names) == 1 and names[0] == keyword


def _wants_all(space) -> bool:
    """True when ``space`` is the ``all`` keyword."""
    return _wants(space, ALL_SPACES)


def resolve_spaces(conn, principal: Principal, *, space=None,
                   space_id=None) -> List[Tuple[str, str]]:
    """Resolve a READ address that may span several namespaces.

    Returns ``[(namespace_id, effective_permission), …]`` — deduped, in the order
    the caller named them (or creation order for ``all``).

    * ``space`` — a namespace name or your alias for one, a list of them, or one
      of the set keywords: ``"all"`` (every namespace you belong to) or ``"*"``
      (every namespace in the deployment, superadmin only).
    * ``space_id`` — a reachable namespace id or a list of them; always
      unambiguous, and the only address left when one name means two spaces.
      ``space`` and ``space_id`` may be combined.
    * neither — one reachable namespace is used silently; SEVERAL is an error.

    Nothing is inferred: there is no default namespace to fall back on, for a
    read or a write. Searching one of several reachable namespaces and returning
    "nothing found" reads as *absence* and is indistinguishable from an answer,
    and a write that guesses is a misfile. A partial result that looks complete
    is the expensive failure, so the caller is asked to say where to look.

    Every address is resolved through :func:`resolve_space`, so scope pinning,
    reachability and the token's permission ceiling are enforced in exactly one
    place — this function only decides WHICH namespaces are in play, never whether
    the caller may have them.
    """
    names, ids = _as_list(space), _as_list(space_id)

    if _wants_all(space) or _wants(space, EVERY_SPACE):
        if ids:
            raise SpaceAmbiguous(
                f"`space='{names[0]}'` already names a whole set of namespaces "
                "— drop `space_id`, or list the namespaces you want explicitly")
        if _wants(space, EVERY_SPACE):
            return _every_namespace(conn, principal)
        _refuse_ambiguous_all(conn, principal, ALL_SPACES)
        return _all_reachable(conn, principal)

    if not names and not ids:
        return [_sole_reachable(conn, principal)]

    out: List[Tuple[str, str]] = []
    seen: set = set()
    for nsid in ids:
        _collect(out, seen, resolve_space(conn, principal, space_id=nsid))
    for name in names:
        _collect(out, seen, resolve_space(conn, principal, space=name))
    return out


def _collect(out: List[Tuple[str, str]], seen: set, resolved: Tuple[str, str]) -> None:
    """Append unless this namespace is already in play (the same space can be
    reached twice — by id and by name — and must not be searched twice)."""
    nsid, perm = resolved
    if nsid not in seen:
        seen.add(nsid)
        out.append((nsid, perm))


def _no_such_name(names: Sequence[str], keyword: str) -> None:
    """Refuse a set keyword that is ALSO the name of a namespace in play.

    A namespace really can be called `mine`, and then the word means two things
    at once. Nobody can tell which was meant, so neither is assumed."""
    if keyword in names:
        raise SpaceAmbiguous(
            f"a namespace here is actually named '{keyword}', so the keyword is "
            f"ambiguous — address that one by `space_id`")


def _unreached_count(conn, principal: Principal) -> int:
    """How many namespaces the caller could read but is not a member of.

    Zero for everyone but a superadmin, whose reach is defined by its role
    rather than by membership rows — which is precisely why `all` has to be
    disambiguated for it and for nobody else.
    """
    if not principal.is_admin or principal.user_id is None:
        return 0
    if principal.scope_namespace_id is not None:
        return 0                              # pinned to one; nothing is outside
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM namespace n WHERE n.owner_user_id <> %(u)s "
            "AND NOT EXISTS (SELECT 1 FROM namespace_member m "
            "                WHERE m.namespace_id=n.id AND m.user_id=%(u)s)",
            {"u": principal.user_id})
        return int(cur.fetchone()[0])


def _refuse_ambiguous_all(conn, principal: Principal,
                          said: Optional[str] = None) -> None:
    """A superadmin read that would answer with less than the role can read.

    For every other caller `all` IS everything, and the word stays untouched.
    For a superadmin it is two different questions, and the narrow answer looks
    exactly like the wide one — a search returning nothing reads as "there is
    nothing", not as "not where I looked". So it is refused, the wide word is
    named, and it is refused only while the two answers differ: a superadmin
    whose memberships already cover the deployment sees no change.

    ``said`` is the word the caller used, or None when they named no namespace
    at all — the same trap, reached by saying nothing.
    """
    outside = _unreached_count(conn, principal)
    if not outside:
        return
    own = [s["name"] for s in list_spaces(conn, principal.user_id)]
    listed = ", ".join(repr(n) for n in sorted(own)) or "none"
    opening = (f"'{said}' is ambiguous here" if said
               else "naming no namespace would answer too narrowly here")
    raise SpaceAmbiguous(
        f"you are a superadmin, so {opening}: you belong to {len(own)} "
        f"namespace(s) ({listed}), and {outside} more exist that your role can "
        f"also read. Say `space='{EVERY_SPACE}'` for every namespace in this "
        f"deployment, or name the ones you mean with `space=[…]` / "
        f"`space_id=[…]`")


def _all_reachable(conn, principal: Principal,
                   keyword: str = ALL_SPACES) -> List[Tuple[str, str]]:
    """Every namespace the caller reaches, capped by the token ceiling."""
    if principal.user_id is None:
        # An env break-glass root has no membership rows to enumerate, and a
        # provisional open-mode token owns nothing yet. Neither can say "all".
        raise AuthError("this token must address a namespace by id")
    ceiling = principal.permission
    if principal.scope_namespace_id is not None:
        # A scoped token reaches exactly one namespace by construction; `all`
        # must not widen it. Resolving through the single-space path keeps the
        # reachability re-check that scope pinning relies on.
        return [resolve_space(conn, principal,
                              space_id=principal.scope_namespace_id)]
    reachable = list_spaces(conn, principal.user_id)
    if not reachable:
        raise SpaceNotFound("you can reach no namespaces yet")
    _no_such_name([s["name"] for s in reachable], keyword)
    return [(s["id"], perm_min(s["permission"], ceiling)) for s in reachable]


def _every_namespace(conn, principal: Principal) -> List[Tuple[str, str]]:
    """Every namespace in the deployment — the superadmin's explicit wide read.

    The counterpart to refusing `all` for a superadmin: having said that the
    narrow answer must not be given silently, there has to be a way to ask for
    the wide one. It is the same reach `resolve_space(space_id=…)` already grants
    that role one namespace at a time, so it adds no authority — only a way to
    spend it in one call instead of N.
    """
    # The name check comes FIRST, and against the caller's OWN reachable set.
    # Two reasons, both learned the hard way:
    #   * a caller who owns a namespace literally named `*` most likely means
    #     that one, and telling them the keyword is superadmin-only would be an
    #     answer to a question they did not ask;
    #   * checking the whole `namespace` table instead — which this did — let
    #     any tenant disable the keyword for the superadmin, deployment-wide,
    #     by creating a namespace named `*`. In `open` mode a self-minted token
    #     can do that unprompted, and the two refusals then point at each other
    #     ("use `*`" ↔ "address it by id"), leaving the operator enumerating
    #     uuids. A stranger's choice of name must not reach into what this
    #     caller's words mean.
    if principal.user_id is not None:
        _no_such_name([s["name"] for s in list_spaces(conn, principal.user_id)],
                      EVERY_SPACE)
    if not principal.is_admin:
        raise AuthError(
            f"`space='{EVERY_SPACE}'` means every namespace in this deployment "
            f"and is superadmin-only — use '{ALL_SPACES}' for the ones you reach")
    if principal.scope_namespace_id is not None:
        # The pin is a property of THIS credential and outranks the role: a
        # token deliberately narrowed to one namespace does not widen back.
        return [resolve_space(conn, principal,
                              space_id=principal.scope_namespace_id)]
    ceiling = principal.permission
    with conn.cursor() as cur:
        cur.execute("SELECT id::text, name FROM namespace ORDER BY created_at, id")
        rows = cur.fetchall()
    if not rows:
        raise SpaceNotFound("this deployment has no namespaces yet")
    return [(r[0], perm_min("admin", ceiling)) for r in rows]


def _sole_reachable(conn, principal: Principal) -> Tuple[str, str]:
    """The caller's only namespace, or an error naming the candidates.

    This is the READ path — a search that named no namespace at all. It carries
    the same superadmin refusal as `all`, and for the same reason: with one
    membership and other namespaces on the deployment, "your only namespace"
    silently answers a narrower question than the caller asked, and an empty
    result reads as "there is nothing". (The WRITE path deliberately keeps
    resolving to the single membership: a write has to land somewhere, the one
    namespace you belong to is the only sane target, and nothing is silently
    left out of an answer.)
    """
    if principal.user_id is None:
        if principal.is_admin:            # env break-glass root owns nothing
            raise AuthError("global admin must address a space by id")
        raise SpaceNotFound("token has no namespaces yet")
    if principal.scope_namespace_id is not None:
        return resolve_space(conn, principal,
                             space_id=principal.scope_namespace_id)
    _refuse_ambiguous_all(conn, principal)
    reachable = list_spaces(conn, principal.user_id)
    if len(reachable) == 1:
        only = reachable[0]
        return only["id"], perm_min(only["permission"], principal.permission)
    if not reachable:
        raise SpaceNotFound("you can reach no namespaces yet")
    names = ", ".join(sorted(s["name"] for s in reachable))
    raise SpaceAmbiguous(
        f"you can reach {len(reachable)} namespaces ({names}) — searching one of "
        f"them silently would look like an answer, so say which: `space` with one "
        f"or more names, or `space='{ALL_SPACES}'` for all of them")


# ─── management: users / namespaces / members ────────────────────────────────
# The fields that say who a person is, as opposed to what their account may do.
# Listed once so create, edit and every read agree on the set.
PROFILE_FIELDS = ("email", "full_name", "department", "position")


def create_user(conn, name: str = "", description: str = "",
                role: str = "user", *,
                can_create_namespace: bool = False, **profile) -> str:
    """Create a user. It owns nothing yet: give it a namespace, share one with
    it, or grant `can_create_namespace` so it can make its own.

    ``profile`` takes any of :data:`PROFILE_FIELDS` — who the person is, which
    is what makes an authorship line in `blame` readable rather than a uuid."""
    if role not in SERVICE_ROLES:
        raise ValueError(f"bad role: {role}")
    unknown = set(profile) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"unknown profile field(s): {', '.join(sorted(unknown))}")
    cols = ["name", "description", "role", "can_create_namespace"]
    vals = [name, description, role, can_create_namespace]
    for f in PROFILE_FIELDS:
        if profile.get(f) is not None:
            cols.append(f)
            vals.append(profile[f])
    placeholders = ", ".join(["%s"] * len(cols))
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO app_user ({', '.join(cols)}) "
                    f"VALUES ({placeholders}) RETURNING id", vals)
        return str(cur.fetchone()[0])


def edit_user(conn, user_id: str, **profile) -> None:
    """Change a user's profile fields. Only the ones passed are touched, so a
    partial update cannot blank the rest."""
    unknown = set(profile) - set(PROFILE_FIELDS) - {"name", "description"}
    if unknown:
        raise ValueError(f"unknown profile field(s): {', '.join(sorted(unknown))}")
    sets, vals = [], []
    for f, v in profile.items():
        if v is not None:
            sets.append(f"{f}=%s")
            vals.append(v)
    with conn.cursor() as cur:
        if not sets:
            # Nothing to change still has to be true OF SOMEBODY: reporting
            # success for an id that does not exist is a lie a caller acts on.
            cur.execute("SELECT 1 FROM app_user WHERE id=%s", (user_id,))
            if cur.fetchone() is None:
                raise SpaceNotFound(f"no such user {user_id}")
            return
        cur.execute(f"UPDATE app_user SET {', '.join(sets)} WHERE id=%s",
                    vals + [user_id])
        if cur.rowcount == 0:
            raise SpaceNotFound(f"no such user {user_id}")


def set_can_create_namespace(conn, user_id: str, allowed: bool) -> None:
    """Grant or withdraw the right to create namespaces."""
    with conn.cursor() as cur:
        cur.execute("UPDATE app_user SET can_create_namespace=%s WHERE id=%s",
                    (allowed, user_id))
        if cur.rowcount == 0:
            raise SpaceNotFound(f"no such user {user_id}")


def list_users(conn, *, role: Optional[str] = None, limit: Optional[int] = None,
               offset: int = 0) -> List[dict]:
    """Users with their service role — control-plane metadata, never a secret.

    Paginated because the result lands in an LLM's context on one door and in a
    web page on the other, and because on a shared deployment an unbounded user
    list is an enumeration surface. ``limit=None`` returns everything, for the
    CLI. Ordered by ``created_at, id`` so paging is stable.
    """
    if role is not None and role not in SERVICE_ROLES:
        raise ValueError(f"bad role: {role}")
    sql = ("SELECT id, name, description, role, can_create_namespace, "
           "created_at, email, full_name, department, position FROM app_user")
    args: list = []
    if role is not None:
        sql += " WHERE role=%s"                # backed by app_user_role_idx
        args.append(role)
    sql += " ORDER BY created_at, id"
    if limit is not None:
        sql += " LIMIT %s"
        args.append(limit)
    if offset:
        sql += " OFFSET %s"
        args.append(offset)
    cols = ["id", "name", "description", "role", "can_create_namespace",
            "created_at", *PROFILE_FIELDS]
    with conn.cursor() as cur:
        cur.execute(sql, args)
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["id"] = str(d["id"])
            out.append(d)
        return out


# ─── service roles (control plane; see SERVICE_ROLES) ────────────────────────
def count_service_admins(conn) -> int:
    """How many users hold an admin role (user_manager or superadmin). Zero ⇒ a
    fresh install with no control plane — the trigger for bootstrap seeding."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app_user WHERE role IN "
                    "('user_manager','superadmin')")
        return int(cur.fetchone()[0])


def count_superadmins(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app_user WHERE role='superadmin'")
        return int(cur.fetchone()[0])


def get_role(conn, user_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM app_user WHERE id=%s", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


def set_role(conn, user_id: str, role: str) -> None:
    """Set a user's service role directly. Callers that lower a superadmin must
    guard against lockout themselves; :func:`revoke_superadmin` does that."""
    if role not in SERVICE_ROLES:
        raise ValueError(f"bad role: {role}")
    with conn.cursor() as cur:
        cur.execute("UPDATE app_user SET role=%s WHERE id=%s", (role, user_id))
        if cur.rowcount == 0:
            raise SpaceNotFound(f"no such user {user_id}")


def grant_superadmin(conn, user_id: str) -> None:
    set_role(conn, user_id, "superadmin")


def revoke_superadmin(conn, user_id: str, *, demote_to: str = "user") -> None:
    """Drop a user out of the superadmin role. Anti-lockout: refuses to remove
    the **last** superadmin (recover such a lockout via the grant CLI)."""
    if demote_to not in SERVICE_ROLES or demote_to == "superadmin":
        raise ValueError(f"bad demote target: {demote_to}")
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM app_user WHERE id=%s", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise SpaceNotFound(f"no such user {user_id}")
        if row[0] != "superadmin":
            return                        # nothing to revoke
        cur.execute("SELECT count(*) FROM app_user WHERE role='superadmin'")
        if int(cur.fetchone()[0]) <= 1:
            raise AuthError("cannot revoke the last superadmin")
        cur.execute("UPDATE app_user SET role=%s WHERE id=%s", (demote_to, user_id))


# How many namespaces one account may own. Not a business rule — a bound, so a
# self-service deployment cannot be turned into an INSERT loop by anyone holding
# a well-formed token.
MAX_NAMESPACES_PER_USER = 50


def create_namespace(conn, owner_user_id: str, name: str, *,
                     description: str = "", instruction: str = "") -> str:
    """Create (or return the existing) namespace ``name`` owned by the user.

    Refuses a name the owner already uses as an alias: the new namespace would
    be born unaddressable by its own name, and — worse — every existing call
    saying that name would go on resolving to the ALIASED space, silently. This
    check lives here, at the single point every door funnels through, rather than
    at the doors: it was written on the self-service door first and the two
    admin-side doors did not have it, which is exactly the duplication this
    codebase keeps paying for.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT namespace_id FROM namespace_alias "
                    "WHERE user_id=%s AND alias=%s", (owner_user_id, name))
        if cur.fetchone() is not None:
            raise SpaceAmbiguous(
                f"'{name}' is already one of that user's aliases — the new "
                "namespace could not be addressed by its own name, and their "
                "existing calls using it would keep resolving elsewhere. Drop "
                "the alias or choose another name")
        cur.execute("SELECT count(*) FROM namespace WHERE owner_user_id=%s",
                    (owner_user_id,))
        if cur.fetchone()[0] >= MAX_NAMESPACES_PER_USER:
            raise SpaceAmbiguous(
                f"that account already owns {MAX_NAMESPACES_PER_USER} "
                "namespaces, which is the cap")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO namespace (owner_user_id, name, description, instruction) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (owner_user_id, name) DO NOTHING "
            "RETURNING id", (owner_user_id, name, description, instruction))
        row = cur.fetchone()
        if row is not None:
            return str(row[0])
        cur.execute("SELECT id FROM namespace WHERE owner_user_id=%s AND name=%s",
                    (owner_user_id, name))
        return str(cur.fetchone()[0])


def create_alias(conn, user_id: str, alias: str, namespace_id: str) -> None:
    """Give a namespace a name of your own.

    Two guards, and each refuses a collision YOU would be creating:

    * the target must already be reachable — otherwise an alias would be a grant
      that walks past membership, which is exactly what the default namespace
      turned out to be;
    * the name must not already resolve for you — an alias that shadows a
      working name silently changes where existing calls land.

    The collision it cannot prevent is the one a stranger makes by sharing a
    namespace named like your alias. That one resolves in your favour (see
    ``_resolve_name``), because your deliberate label should outlive someone
    else's naming.
    """
    with conn.cursor() as cur:
        if _reach(cur, user_id, namespace_id) is None:
            raise SpaceNotFound(
                f"namespace {namespace_id} is not reachable — an alias names a "
                "space you already have, it does not grant one")
        try:
            existing = _resolve_name(cur, user_id, alias)
        except (SpaceNotFound, SpaceAmbiguous):
            existing = None
        if existing is not None:
            raise SpaceAmbiguous(
                f"'{alias}' already resolves for you (namespace {existing}) — "
                "pick another alias, or drop the existing one first")
        cur.execute(
            "INSERT INTO namespace_alias (user_id, alias, namespace_id) "
            "VALUES (%s, %s, %s)", (user_id, alias, namespace_id))


def drop_alias(conn, user_id: str, alias: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM namespace_alias WHERE user_id=%s AND alias=%s",
                    (user_id, alias))
        return cur.rowcount > 0


def list_aliases(conn, user_id: str) -> dict:
    """``{namespace_id: alias}`` for this user."""
    with conn.cursor() as cur:
        cur.execute("SELECT namespace_id, alias FROM namespace_alias "
                    "WHERE user_id=%s", (user_id,))
        return {str(nid): a for nid, a in cur.fetchall()}


def create_own_namespace(conn, principal: Principal, name: str, *,
                         description: str = "", instruction: str = "") -> str:
    """Create a namespace for the caller — the explicit door that replaced lazy
    creation.

    Addressing a namespace that did not exist used to create it, so a mistyped
    name produced a new empty space and the write landed there, looking like it
    had worked. Creation is now something you ask for, which makes a typo an
    error rather than a place — and gives the `can_create_namespace` right a
    single point to be enforced at.
    """
    if principal.user_id is None:
        if not principal.provisional:
            raise AuthError("this token has no owning user")
        # open mode: a token that has never written has no user row yet, and
        # this is now the FIRST thing such a caller does — lazy creation used to
        # materialize them on the way past. Without this a fresh token could
        # never create the namespace it is being told to create.
        ensure_user_for_token(conn, principal)
    if principal.scope_namespace_id is not None:
        raise AuthError("a scoped token cannot create a namespace")
    if not perm_at_least(principal.permission, "write"):
        # A token deliberately weakened to read-only is the configuration the
        # docs recommend for agents; it must not be able to change deployment
        # state, whatever the account behind it is allowed to do.
        raise AuthError(
            f"creating a namespace needs a write-capable token "
            f"(this one grants {principal.permission})")
    if not can_create_namespace(conn, principal):
        raise AuthError("you may not create namespaces — ask an admin to create "
                        "one for you or share theirs")
    # the alias collision and the per-account cap are enforced in
    # `create_namespace`, so every door gets them
    return create_namespace(conn, principal.user_id, name,
                            description=description, instruction=instruction)


def add_member(conn, namespace_id: str, user_id: str, permission: str = "read") -> None:
    if permission not in _RANK:
        raise ValueError(f"bad permission: {permission}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO namespace_member (namespace_id, user_id, permission) "
            "VALUES (%s, %s, %s) ON CONFLICT (namespace_id, user_id) "
            "DO UPDATE SET permission=EXCLUDED.permission",
            (namespace_id, user_id, permission))


def edit_namespace(conn, namespace_id: str, *, description: Optional[str] = None,
                   instruction: Optional[str] = None) -> None:
    sets, params = [], []
    if description is not None:
        sets.append("description=%s"); params.append(description)
    if instruction is not None:
        sets.append("instruction=%s"); params.append(instruction)
    if not sets:
        return
    params.append(namespace_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE namespace SET {', '.join(sets)} WHERE id=%s", params)


def list_spaces(conn, user_id: str) -> List[dict]:
    """Every namespace the user can reach: owned + shared, with flags."""
    aliases = list_aliases(conn, user_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.id, n.name, n.description, n.instruction, n.owner_user_id, "
            "       CASE WHEN n.owner_user_id=%(u)s THEN 'admin' ELSE m.permission END "
            "FROM namespace n "
            "LEFT JOIN namespace_member m ON m.namespace_id=n.id AND m.user_id=%(u)s "
            "WHERE n.owner_user_id=%(u)s OR m.user_id=%(u)s "
            "ORDER BY n.created_at", {"u": user_id})
        out = []
        for nid, name, desc, instr, owner, perm in cur.fetchall():
            out.append({
                "id": str(nid), "name": name, "description": desc,
                "instruction": instr, "permission": perm,
                "mine": str(owner) == user_id,
                # what to type for this space: your alias if you gave it one,
                # else its name — which may not be unique among what you reach,
                # hence the alias in the first place.
                "alias": aliases.get(str(nid)),
            })
        return out


def list_namespaces(conn, *, owner_user_id: Optional[str] = None,
                    limit: Optional[int] = None, offset: int = 0) -> List[dict]:
    """Every namespace on the deployment — the operator's inventory.

    `list_spaces` answers "what can *I* reach" and is caller-relative; there was
    no way to ask "what exists". After a dozen namespaces are provisioned, an
    admin otherwise has to remember uuids to administer them.
    """
    sql = ("SELECT id, name, description, instruction, owner_user_id, created_at "
           "FROM namespace")
    args: list = []
    if owner_user_id is not None:
        sql += " WHERE owner_user_id=%s"
        args.append(owner_user_id)
    sql += " ORDER BY created_at, id"
    if limit is not None:
        sql += " LIMIT %s"
        args.append(limit)
    if offset:
        sql += " OFFSET %s"
        args.append(offset)
    cols = ["id", "name", "description", "instruction", "owner_user_id",
            "created_at"]
    with conn.cursor() as cur:
        cur.execute(sql, args)
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["id"] = str(d["id"])
            d["owner_user_id"] = str(d["owner_user_id"])
            out.append(d)
        return out


def list_members(conn, namespace_id: str) -> List[dict]:
    """Who can reach a namespace: its owner, then everyone shared in.

    The owner is synthesized rather than stored in `namespace_member`, so a
    caller reading membership sees the whole picture — "who can see this?" is
    the question a public/private split raises, and a list that silently omits
    the owner answers it wrongly.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT owner_user_id FROM namespace WHERE id=%s",
                    (namespace_id,))
        row = cur.fetchone()
        if row is None:
            raise SpaceNotFound(f"no such namespace {namespace_id}")
        out = [{"user_id": str(row[0]), "permission": "admin", "owner": True,
                "created_at": None}]
        cur.execute("SELECT user_id, permission, created_at FROM namespace_member "
                    "WHERE namespace_id=%s ORDER BY created_at", (namespace_id,))
        for uid, perm, created in cur.fetchall():
            out.append({"user_id": str(uid), "permission": perm,
                        "owner": False, "created_at": created})
        return out


# ─── management: tokens ──────────────────────────────────────────────────────
def issue_token(conn, user_id: str, *, namespace_id: Optional[str] = None,
                permission: str = "write", label: str = "",
                expires_at=None) -> Tuple[str, str]:
    """Mint a token for a user. Returns ``(secret, token_id)``; the secret is
    shown once and stored only as a hash. ``namespace_id`` scopes it to one
    space; ``permission`` is its ceiling."""
    if permission not in _RANK:
        raise ValueError(f"bad permission: {permission}")
    secret = new_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO token (token_hash, user_id, namespace_id, permission, "
            "label, expires_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (token_hash(secret), user_id, namespace_id, permission, label, expires_at))
        return secret, str(cur.fetchone()[0])


def register_token(conn, user_id: str, secret: str, *,
                   namespace_id: Optional[str] = None, permission: str = "write",
                   label: str = "", expires_at=None) -> str:
    """Store a bring-your-own token (open mode). Validates format, hashes it."""
    if not valid_format(secret):
        raise ValueError("bring-your-own token must match mgk_ + 43 url-safe chars")
    if permission not in _RANK:
        raise ValueError(f"bad permission: {permission}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO token (token_hash, user_id, namespace_id, permission, "
            "label, expires_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (token_hash(secret), user_id, namespace_id, permission, label, expires_at))
        return str(cur.fetchone()[0])


def revoke_token(conn, token_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("UPDATE token SET revoked_at=now() "
                    "WHERE id=%s AND revoked_at IS NULL", (token_id,))
        return cur.rowcount > 0


def token_owner(conn, token_id: str) -> Optional[str]:
    """Which user a token belongs to, or None if there is no such token.

    Needed to authorize an action addressed by *token* rather than by user: the
    caller's right to touch it follows from whose token it is.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM token WHERE id=%s", (token_id,))
        row = cur.fetchone()
    return str(row[0]) if row else None


def list_tokens(conn, user_id: str) -> List[dict]:
    """A user's tokens — metadata only, never the secret."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, namespace_id, permission, label, expires_at, revoked_at, "
            "       last_used_at, created_at FROM token WHERE user_id=%s "
            "ORDER BY created_at", (user_id,))
        cols = ["id", "namespace_id", "permission", "label", "expires_at",
                "revoked_at", "last_used_at", "created_at"]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["id"] = str(d["id"])
            d["namespace_id"] = str(d["namespace_id"]) if d["namespace_id"] else None
            d["active"] = d["revoked_at"] is None
            out.append(d)
        return out


# ─── request-access: join an existing namespace ──────────────────────────────
def reaches(conn, user_id: str, namespace_id: str) -> Optional[str]:
    """The user's effective permission on a namespace, or None. The public form
    of :func:`_reach`, for callers outside this module that must ask about
    reachability without going through an address resolver that raises."""
    # The NORMALIZED value is what goes to Postgres. `uuid.UUID` accepts forms
    # Postgres does not (`urn:uuid:…`), so passing the raw string through would
    # still produce the driver fault — and abort the caller's transaction —
    # that this check exists to prevent.
    namespace_id = _as_uuid(namespace_id)
    with conn.cursor() as cur:
        return _reach(cur, user_id, namespace_id)


def _as_uuid(value: str) -> str:
    """``value`` as a uuid string, or a plain ValueError.

    Handing Postgres a malformed uuid aborts the transaction with a driver
    error, which reads to the caller as a server fault rather than a bad
    argument. Checked here so every id-taking entry point fails the same way.
    """
    import uuid as _uuid
    try:
        return str(_uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"not a namespace id: {value!r}")


# How many open requests one ACCOUNT may have outstanding. The row is recorded
# whether or not the namespace exists (see below), so without a cap an account
# could grow the table one guessed uuid at a time.
#
# It bounds an account, not an adversary: in `open` mode anyone can mint a token
# and materialize a fresh account, so the real bound is the cap times however
# many accounts they care to create. That is the same shape as
# MAX_NAMESPACES_PER_USER and inherent to self-service mode — the cap is a
# guard-rail on a table, not a defence against someone determined to fill it.
MAX_PENDING_REQUESTS_PER_USER = 100


def request_access(conn, requester_user_id: str, namespace_id: str,
                   permission: str = "read") -> str:
    """Record a request to join a namespace, and return the request's id.

    **The row is written whether or not the namespace exists, and that is the
    whole point.** The insert used to be conditional: a real namespace the
    requester cannot reach produced a request, a uuid naming nothing produced a
    foreign-key violation. That difference was a membership-blind existence
    oracle. Answering identically was the first fix — but a check-then-write
    still *runs* differently in the two cases, and the measured gap was ~8× with
    no overlap, so the oracle simply moved into the clock. Two answers are only
    the same when the same work produces them.

    So `access_request.namespace_id` no longer carries a foreign key (migration
    0014) and every request is upserted. A row pointing at nothing is inert:
    `list_requests` selects by namespace, so no one can ever see it, and
    `decide_access` resolves the namespace and refuses. What remains is a table
    an account could grow by guessing — hence the cap above, which also bounds
    the request spam that was always possible against real namespaces.

    The caller is `admin.request_access`, which returns a receipt WITHOUT this
    id: the requester has no use for it, and handing one back would restore the
    difference this removes.
    """
    if permission not in _RANK:
        raise ValueError(f"bad permission: {permission}")
    namespace_id = _as_uuid(namespace_id)
    with conn.cursor() as cur:
        # Amending a request you already hold adds no row, so the cap does not
        # apply to it — otherwise a caller at the limit could not even lower a
        # pending `admin` request to `read`. This asks about the caller's own
        # requests, never about what exists.
        cur.execute("SELECT 1 FROM access_request "
                    "WHERE requester_user_id=%s AND namespace_id=%s",
                    (requester_user_id, namespace_id))
        if cur.fetchone() is None:
            cur.execute("SELECT count(*) FROM access_request "
                        "WHERE requester_user_id=%s AND status='pending'",
                        (requester_user_id,))
            if cur.fetchone()[0] >= MAX_PENDING_REQUESTS_PER_USER:
                raise ValueError(
                    f"you already have {MAX_PENDING_REQUESTS_PER_USER} requests "
                    "waiting to be decided, which is the cap")
        cur.execute(
            "INSERT INTO access_request (requester_user_id, namespace_id, "
            "requested_permission) VALUES (%s, %s, %s) "
            "ON CONFLICT (requester_user_id, namespace_id) DO UPDATE "
            "SET requested_permission=EXCLUDED.requested_permission, "
            "    status='pending', decided_at=NULL RETURNING id",
            (requester_user_id, namespace_id, permission))
        return str(cur.fetchone()[0])


def list_requests(conn, namespace_id: str, *, pending_only: bool = True) -> List[dict]:
    q = ("SELECT id, requester_user_id, requested_permission, status, created_at, "
         "decided_at FROM access_request WHERE namespace_id=%s")
    if pending_only:
        q += " AND status='pending'"
    q += " ORDER BY created_at"
    with conn.cursor() as cur:
        cur.execute(q, (namespace_id,))
        cols = ["id", "requester_user_id", "requested_permission", "status",
                "created_at", "decided_at"]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["id"] = str(d["id"])
            d["requester_user_id"] = str(d["requester_user_id"])
            out.append(d)
        return out


def approve_request(conn, request_id: str) -> None:
    """Grant the requested membership and close the request."""
    with conn.cursor() as cur:
        cur.execute("SELECT requester_user_id, namespace_id, requested_permission "
                    "FROM access_request WHERE id=%s AND status='pending'",
                    (request_id,))
        row = cur.fetchone()
        if row is None:
            raise SpaceNotFound(f"no pending request {request_id}")
        requester, nsid, perm = row
    add_member(conn, str(nsid), str(requester), perm)
    with conn.cursor() as cur:
        cur.execute("UPDATE access_request SET status='approved', decided_at=now() "
                    "WHERE id=%s", (request_id,))


def deny_request(conn, request_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE access_request SET status='denied', decided_at=now() "
                    "WHERE id=%s AND status='pending'", (request_id,))
