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

- **user** — an account. Owns namespaces and holds tokens. Has one *default*
  namespace used when a call doesn't name a space.
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

A global **admin token** (`MEMGRES_ADMIN_TOKEN`) can provision anything.

## Addressing a space

A namespace **id** (a uuid) is the canonical, unambiguous address. A **name** is
a convenience that resolves **only among your own** namespaces — so a name can
never collide with a namespace someone shared with you (that one is reached by
id). In practice:

- your own space → pass `space="notes"` (a name; owner is implied by your token);
- a shared space → pass `space_id="<uuid>"` (from `list_spaces`);
- neither → your **default** namespace.

Every read/write API takes optional `space` and `space_id`.

## Getting a token

**open mode — bring your own.** Generate one yourself (any `mgk_` + 43 url-safe
chars) and just start using it; the user + a `default` namespace are created
lazily on your **first write** (never on a read, so probing creates nothing).

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

# write into a named space (created lazily the first time)
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

Point your MCP client at the server (stdio or the Streamable-HTTP endpoint) and
pass `token` as a tool argument. Tools:

- `memory_write` / `memory_get` / `memory_recall` / `memory_move` /
  `memory_history` / `memory_blame` / `memory_forget` — all take `space` /
  `space_id`;
- `memory_list_spaces` — your reachable namespaces (id, name, permission, default);
- `memory_issue_token` — mint a token (rotate/delegate; secret returned once);
- `memory_list_tokens` / `memory_revoke_token` — manage them.

For a single-tenant MCP endpoint you can set `MEMGRES_TOKEN` once and omit the
per-call `token`.

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

## Admin provisioning (managed mode)

With `MEMGRES_ADMIN_TOKEN` set, provision users, namespaces and tokens:

```bash
A='-H "Authorization: Bearer $MEMGRES_ADMIN_TOKEN"'

# create a user, a namespace they own, and a token for them
UID=$(curl -s $BASE/admin/users -d '{"name":"alice"}' $A | jq -r .id)
NS=$(curl -s $BASE/admin/namespaces \
      -d "{\"owner_user_id\":\"$UID\",\"name\":\"team\"}" $A | jq -r .id)
curl -s $BASE/admin/tokens \
  -d "{\"user_id\":\"$UID\",\"permission\":\"write\"}" $A     # → token (once)

# add another user as a member; revoke a token
curl -s $BASE/admin/namespaces/$NS/members -d '{"user_id":"…","permission":"read"}' $A
curl -s $BASE/admin/tokens/<token_id>/revoke $A
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
| `MEMGRES_ADMIN_TOKEN` | global admin bearer for provisioning |
| `MEMGRES_TOKEN` | a default token used when a call passes none (single-tenant endpoints) |
