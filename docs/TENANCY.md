# Tenancy: users, namespaces & tokens

memgres can run three ways, set by `MEMGRES_KEY_MODE`:

| mode | who can write | when to use |
|---|---|---|
| `single` (default) | anyone; one shared space, no auth | embedded / single-agent |
| `open` | anyone with a well-formed token; the token self-registers on first write | self-serve, bring-your-own-token |
| `managed` | only tokens an admin provisioned | controlled multi-tenant |

In `single` mode nothing below applies — there is one space, no token needed.
The rest of this document is about `open` and `managed`.

## The model

- **user** — an account. Owns namespaces and holds tokens. Where its calls land
  is never inferred: with one reachable namespace there is nothing to choose,
  and beyond that it says which.
- **namespace** — an isolation unit owned by a user. Memories in different
  namespaces are never co-searchable. A namespace carries a `description` and an
  `instruction` (free text telling an agent how to use it).
- **token** — a bearer credential that authenticates *as a user*. Rotatable,
  expirable, revocable, and restrictable (a permission ceiling + optional scope
  to a single namespace). Format: `mgk_` followed by 43 url-safe characters;
  stored only as a hash and shown once when issued.

Two independent axes — don't confuse them:

- **Organize** memories *inside* one namespace → the **tree** (`path`) and
  **tags**. Search (lexical and semantic) can be scoped to any subtree. One agent
  organizes with the tree; it does **not** need extra namespaces for that.
- **Isolate** between accounts → the **namespace**.

## Permissions

| on a namespace | can |
|---|---|
| `read` | recall / get / history / blame / at |
| `write` (⊃ read) | + create / edit / move / forget |
| `admin` (⊃ write) | + manage tokens & members, edit description/instruction |

A token also has its own ceiling. Effective permission on a space is
`min(your membership there, the token's ceiling)`. A read-only token never
writes, even in a namespace you own.

Beyond per-namespace membership, a user has a **service role** (`app_user.role`)
that governs the control plane:

| role | can |
|---|---|
| `user` (default) | own namespaces; manage access to its **own** spaces (approve requests) |
| `user_manager` | + create users and (re)issue tokens — provisioning only, **no** cross-tenant data access |
| `superadmin` | + full root: read/write **any** namespace, add members anywhere, grant/revoke roles |

A `superadmin`'s data access is capped by its token ceiling like anyone's; a
`user_manager` never gains implicit access to another tenant's memories.

## Addressing a space

A namespace **id** (a uuid) is the canonical, unambiguous address. A **name** is
a convenience that resolves **only among your own** namespaces — so a name can
never collide with a namespace someone shared with you (that one is reached by
id). In practice:

- by **name** → `space="notes"`: your alias for a space, or any space you reach
  that carries that name;
- by **id** → `space_id="<uuid>"` (from `list_spaces`), always unambiguous;
- **neither** → your token's scope, or your single reachable namespace. Reaching
  several and naming none is an error, not a guess.

Every read/write API takes optional `space` and `space_id`. **Nothing is created
by being addressed**: a name that matches nothing is an error, so a typo is a
mistake rather than a new, empty, plausible-looking space your write lands in.
Ask for one with `POST /spaces` (`memory_create_space`), which needs the right to
create namespaces.

### When a name means two things

A namespace's name is unique per owner, so two people may each own `notes` — and
once one of them shares theirs with you, the bare name means two things in your
account. That collision is made by *someone else's* act of sharing, after you had
already named your own spaces, so it cannot be refused when a namespace is
created without letting your names block other people from sharing. It is
refused when you address it, with both candidates named, and you settle it with
an **alias**:

```bash
curl -s $BASE/spaces/aliases -d '{"alias":"bobs-notes","space_id":"<uuid>"}'
curl -s -X DELETE $BASE/spaces/aliases/bobs-notes
```

An alias is private to you and **grants nothing** — the space has to be reachable
already. Two collisions are refused up front because they would be yours to
cause: an alias that shadows a name that already works for you, and a namespace
born with the name of one of your aliases. The one that remains — a stranger
sharing a space named like your alias — resolves in favour of your alias, since
a deliberate label of yours should not be broken by someone else's naming.

### Searching several spaces

A search (`recall`, `find`, the `list` browse) may span namespaces. `space` takes
a name, a list of names, or the keyword `"all"`; `space_id` takes ids, and the
two combine. Over HTTP they are repeated query parameters:

```bash
curl -s "$BASE/recall?q=pricing&space=work&space=notes"   # two of yours
curl -s "$BASE/recall?q=pricing&space=all"                 # everything you reach
curl -s "$BASE/recall?q=pricing&space=work&space_id=<uuid>"  # yours + a shared one
```

Every hit says which namespace answered, via `space` and `space_id`.

**If you reach more than one namespace, you must name one** — for a search as
much as for a write. Searching one of them and answering "nothing found" is
indistinguishable from an answer, and a write that guesses is a misfile. So you
are told which namespaces were meant, and you pick — or, for a search, say
`all`.

(If you happen to own a namespace literally named `all`, the keyword is refused
as ambiguous rather than guessed at; address that one by `space_id`.)

One asymmetry worth knowing: `all` means *every namespace you are a member or
owner of*. A **superadmin** additionally reaches any namespace by id, so for that
one caller `all` covers less than the credential could — deliberately, since a
routine search should not sweep other tenants' data into a context by default.
Address those by `space_id`.

## Addressing a memory

Within a namespace a memory has two addresses: its **id** (a uuid) and its
**path** (the tree address, unique per namespace). `at` takes the path anywhere
an `id` is taken — get, write, move, forget, history, blame. Over HTTP the URL
segment takes either: it is read as an id when it parses as a uuid, and as a
path otherwise — so hyphenated and non-ASCII paths address fine:

```bash
curl -s $BASE/memories/decisions.pricing          # by path
curl -s $BASE/memories/018f…-…                    # by id
curl -s -X PATCH $BASE/memories/decisions.pricing -d '{"body":"…"}'
```

`at` and `path` are different jobs: **`at` finds a memory, `path` says where a
memory lives.** So `at="ops.x"` edits whatever is at that address, while
`path="ops.x"` files a new memory there (and the two together move one).

### When the address has moved

A memory that moves records the address it left, so an old path still resolves.
What happens then depends on what you are doing:

- **reads follow it** and set `moved_from` in the answer — the memory that used
  to live there is what you reached for, and now you know its address changed;
- **writes refuse**, and say where it went. Writing to a stale address means your
  picture is out of date, and both quiet answers commit you to it: edit the moved
  memory and your write lands somewhere you did not name; create at the vacated
  path and you now have two memories on one subject, the second of which you will
  keep writing to while the first lives on elsewhere — with no error at any point.
  `if_moved="follow"` edits it where it is now; `if_moved="create"` claims the
  vacated path for something genuinely new;
- **deletes never follow.** Deleting on the strength of a stale address is the
  one mistake here you cannot undo.

A *deleted* memory leaves no redirect at all — erasure is real, history goes with
the row — so its path is simply free again. That is exactly the case where a
duplicate is impossible, since there is nothing left to duplicate.

## Getting a token

**open mode — bring your own.** Generate one yourself (any `mgk_` + 43 url-safe
chars). The account materializes when you ask for your first namespace
(`POST /spaces`) — never on a read, so probing creates nothing, and never as a
side effect of a write that named a space you mistyped.

```python
import secrets
token = "mgk_" + secrets.token_urlsafe(32)     # save this — it is your identity
```

**managed mode — an admin issues it.** See *Admin provisioning* below.

Either way you can mint more tokens for yourself once you have one (rotate,
delegate a read-only key, time-box a temporary one).

## Using it — HTTP

Send the token as `Authorization: Bearer <token>` (or `X-Memgres-Token`).

```bash
BASE=http://localhost:8080
TOK="mgk_…"

# make the space, then write into it — nothing is created by being named
curl -s $BASE/spaces -H "Authorization: Bearer $TOK" -d '{"name":"work"}'
curl -s $BASE/memories -H "Authorization: Bearer $TOK" \
  -d '{"body":"first note\n","space":"work","tags":["seed"]}'

# recall within that space
curl -s "$BASE/recall?q=note&space=work" -H "Authorization: Bearer $TOK"

# what can this token reach?
curl -s $BASE/spaces -H "Authorization: Bearer $TOK"

# a shared space is addressed by id
curl -s "$BASE/recall?q=note&space_id=<uuid>" -H "Authorization: Bearer $TOK"
```

## Using it — MCP

**The identity is pinned in the client config, not handled by the model.** The
tools take no `token` argument; the server resolves it from:

- the `Authorization: Bearer <mgk_…>` / `X-Memgres-Token` **header** the client
  sends (http transport) — so one shared endpoint serves many clients, each
  pinned to its own user via its own config `headers`;
- else `MEMGRES_TOKEN` — a stdio client sets it in its config `env` block; a
  dedicated http endpoint sets it on the service.

A header/env pin is authoritative (the model can't override it), and a
namespace-scoped token also locks the agent to one space.

Tools:

- `memory_write` / `memory_get` / `memory_recall` / `memory_move` /
  `memory_history` / `memory_blame` / `memory_forget` — all take `space` /
  `space_id`; the id-addressed ones also take `at` (a path);
- `memory_whoami` — what this token may do, as capabilities;
- `memory_list_spaces` — your reachable namespaces (id, name, permission, alias);
- `memory_create_space` / `memory_set_alias` / `memory_drop_alias` — make one,
  and give it a name of your own;
- `memory_issue_token` — mint a token (rotate/delegate; secret returned once);
- `memory_list_tokens` / `memory_revoke_token` — manage them;
- `memory_admin_*` — the control plane (see below), where the caller has the
  authority for it.

Only a genuinely multi-tenant endpoint (`open`/`managed`, **no** pinned token)
exposes a `token` argument for the model to supply; force it either way with
`MEMGRES_MCP_TOKEN_ARG=on|off`. In `single` mode no token is needed at all.

## Sharing a namespace (request-access)

Someone wants into a namespace they don't own:

```bash
# requester asks for read on a namespace id
curl -s $BASE/spaces/<space_id>/access-requests \
  -H "Authorization: Bearer $REQUESTER" -d '{"permission":"read"}'

# an admin of that namespace lists and approves
curl -s $BASE/spaces/<space_id>/access-requests -H "Authorization: Bearer $ADMIN"
curl -s $BASE/access-requests/<req_id>/approve  -H "Authorization: Bearer $ADMIN"
```

After approval the requester's tokens reach the shared space **by id**, at the
granted permission.

## First admin — bootstrap (managed mode)

A fresh managed database has no users, so someone must be seeded before anyone
can be provisioned. At startup, if **zero admin users exist**, memgres seeds the
first one from a bootstrap secret and then goes inert (the secret is never a
standing backdoor). The secret is stored hashed as an ordinary token of the
seeded user, so the same value afterwards authenticates that **real** user —
admin actions are attributable, not anonymous.

Provide the secret one of two ways (setting both is an error):

- `MEMGRES_ADMIN_TOKEN` — the secret itself (must be a strong `mgk_` token).
- `MEMGRES_ADMIN_TOKEN_FILE` — a path, **read-or-create** (Jenkins-style): if the
  file holds a token it's used; if missing/empty, memgres generates one, writes
  it `0600`, and logs the **path only** (never the secret). Copy it out and
  delete the file on your own schedule.

The seeded role is `MEMGRES_ADMIN_ROLE` — `user_manager` (default) or
`superadmin`. A legacy/non-`mgk_` `MEMGRES_ADMIN_TOKEN` is not seeded but still
works as an anonymous break-glass root (with a warning).

Need a superadmin later (you seeded only a `user_manager`, or revoked the last
one)? Use the CLI — it talks straight to the database, so the gate is host/DB
access, not a network token:

```bash
memgres-grant-superadmin --list                # users + roles
memgres-grant-superadmin --user <uuid>         # promote
memgres-grant-superadmin --revoke --user <uuid># demote (anti-lockout applies)
```

## Admin provisioning (managed mode)

An admin-role token (or the env break-glass root) provisions users, namespaces
and tokens. `user_manager` covers users/tokens; cross-tenant membership and role
grants require `superadmin`:

```bash
A='-H "Authorization: Bearer $ADMIN_TOKEN"'

# create a user, a namespace they own, and a token for them
UID=$(curl -s $BASE/admin/users -d '{"name":"alice"}' $A | jq -r .id)
NS=$(curl -s $BASE/admin/namespaces \
      -d "{\"owner_user_id\":\"$UID\",\"name\":\"team\"}" $A | jq -r .id)
curl -s $BASE/admin/tokens \
  -d "{\"user_id\":\"$UID\",\"permission\":\"write\"}" $A     # → token (once)

# add another user as a member; revoke a token       (member add: superadmin)
curl -s $BASE/admin/namespaces/$NS/members -d '{"user_id":"…","permission":"read"}' $A
curl -s $BASE/admin/tokens/<token_id>/revoke $A

# service-role management (superadmin only)
curl -s $BASE/admin/users/$UID/grant-superadmin $A
curl -s $BASE/admin/users/$UID/revoke-superadmin -d '{"demote_to":"user"}' $A
```

The same operations are available over MCP as `memory_admin_*` tools, over the
same service layer — so an operator working through an MCP client is not pushed
onto curl. `MEMGRES_MCP_ADMIN_TOOLS=on|off|auto` controls whether they are
registered; `auto` registers them wherever there are identities to administer,
which is every mode but `single`. Turning them off shortens an agent-facing tool
list — it is a context economy, **not** a security boundary, since every tool
authorizes when it is called.

### Who may act on whom

**Authority is the role AND the token, never just the role.** A deployment-wide
control-plane act needs an *unscoped, admin-ceiling* token; a per-namespace one
refuses a token scoped to a different namespace. So handing an agent a read-only
or namespace-pinned token really does narrow it, even when the account behind it
is an admin — otherwise the agent could simply issue itself a better credential.

A `user_manager` provisions ordinary accounts: it may act on accounts holding the
plain `user` role, and nothing else. Issuing, revoking or listing tokens for an
account that holds an admin role requires `superadmin` — without that rule, a
`user_manager` could mint a token for a superadmin's account and become data-root
in one request. The last superadmin also cannot be demoted or have their last
token revoked, since that would leave the control plane with nobody in charge.

### Creating namespaces

Bringing a namespace into existence is a right (`can_create_namespace`), separate
from provisioning people: an ordinary member can be trusted to organize their own
corner without also being able to create users. Admin roles always have it. A
user who has no namespace and no right to create one gets an explanation telling
them to ask for one — not a silently-created namespace nobody asked for.

```bash
curl -s $BASE/admin/users/$UID/can-create-namespace -d '{"allowed":true}' $A
curl -s $BASE/whoami -H "Authorization: Bearer $TOKEN"   # capabilities, not a role name
```

## Anti-garbage

`open` mode never creates anything for an anonymous or read-only visitor:
connecting mints nothing, reads create nothing, and a namespace only ever comes
into being via a token's first write. Lose your token → you lose access to your
own data (there is no recovery), but the system is never littered with orphaned
registrations.

## Environment

| var | meaning |
|---|---|
| `MEMGRES_KEY_MODE` | `single` (default) \| `open` \| `managed` |
| `MEMGRES_ADMIN_TOKEN` | bootstrap/break-glass bearer (managed): seeds the first admin, then resolves to that user |
| `MEMGRES_ADMIN_TOKEN_FILE` | read-or-create path for the bootstrap token (mutually exclusive with the above) |
| `MEMGRES_ADMIN_ROLE` | role the bootstrap admin is seeded with: `user_manager` (default) \| `superadmin` |
| `MEMGRES_TOKEN` | a default token used when a call passes none (single-tenant endpoints) |
