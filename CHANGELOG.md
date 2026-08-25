# Changelog

All notable changes to memgres are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor = features/changes,
patch = fixes).

## [0.7.0] — 2026-08-25

Search, tags and retention — three places where memgres answered with silence
where it should have answered with data — plus the beginning of an answer to "is
this still true?", and the link graph the corpus had been writing by hand all
along.

### Links
- **`[[wiki links]]` in a body are now edges you can walk, in both directions.**
  The convention was already load-bearing with no tool behind it: 238 links
  across the 97 memories of the reference corpus, 91% resolving. What text could
  not do was answer "what points HERE" — the question that matters when a fact
  changes — and 42 of those memories had no inbound link at all.
  - `[[path]]`, `[[path#anchor]]`, `[[path|label]]`, `[[path#anchor|label]]`;
    `#` before `|` as in every wiki dialect. `[[idea:slug]]` / `[[file:slug]]`
    point at other stores and are recorded but never resolved here.
  - Edges pin the target's `id` at write time, so a link survives its target
    moving and cannot be hijacked by something later claiming the vacated path.
  - Unresolved is a real state, not an error: a link to something not yet
    written stands as `resolved: false` and binds itself when the target appears
    (or when a memory moves onto that path). A target later erased nulls the
    edge rather than letting it drift onto whatever takes the path next.
  - Code spans and fenced blocks are not scanned — the corpus's own notes
    explain the syntax in backticks, and a validator that flags its own
    documentation teaches everyone to ignore it. URLs and prose stay prose.
  - Links never cross a tenant boundary; the backlink query re-applies the
    namespace predicate rather than trusting that rule.
  - `memory_links` (MCP) / `GET /memories/{id}/links` (REST).
- **The graph is backfilled once, automatically**, and `memgres-relink` forces a
  rebuild. Deriving edges only on write would have upgraded every existing
  corpus into an empty graph that answered "nothing points here" for every
  memory — a fact-shaped silence, which is the failure this release is mostly
  about.

### Breaking
- **`memory_find` is gone.** It searched titles and nothing else, while
  `memory_recall` searched bodies and nothing else, so a caller had to guess which
  half held what it wanted. Lexical recall now matches title OR body and weights a
  title match higher; `bodies=false` gives the light "where is it" rows `find`
  returned. Same for `GET /find` and `Store.find`.
- **`ttl_days` is gone from `memory_write`** (MCP, REST and `Store.write`).
  Retention is the operator's promise about how long client data is kept, and a
  per-write override made it advisory: `0` was read as "keep forever", a larger
  value outran the policy, and an edit that omitted it silently cleared an expiry
  already set. `MEMGRES_RETENTION_DAYS` is now the only thing that decides.
- **A write that stores content must supply `title`** (`MEMGRES_REQUIRE_TITLE`,
  default on). `move`/`retag` are exempt — they store no content.
- **Compatibility floor 14 → 16.** Migration 0015 normalises stored tags; a client
  older than 0.7.0 neither normalises nor knows to, so its filter for `X402` would
  silently miss a row now stored as `x402`. Upgrade every client of a database
  together.

### Added
- **`valid_at`** on a write: the day the content was last known to be ACCURATE.
  `created_at`/`updated_at` say when a row was written, which cannot answer "is
  this still true" — a typo fix moves `updated_at`, and a fact distilled today
  from a 2021 letter is not fresh because the row is new. It lives on the history
  row (a property of one assertion, like `source`), folds into the hash chain, may
  point into the past, and sending it alone records a re-confirmation as
  `op: revalidate` rather than forcing a fake edit.
- **`memory_tags`** — the tag vocabulary in use, most-used first, narrowable by
  prefix. A writer cannot reuse a label it has never seen.
- **`match_tags: all | any`** on recall and browse (`@>` vs `&&`, same index).
- **Retention is actually enforced.** `purge_expired` had no caller anywhere in the
  tree: expired rows were hidden from reads and never deleted, so "we expire your
  data after N days" was half a promise. It now runs on a timer
  (`MEMGRES_RETENTION_SWEEP_INTERVAL`) and takes chunk vectors with it — pgvector
  cascades on the FK, but qdrant is out-of-band and orphaned points would keep
  taking candidate slots until `fetch_hit_rows` found no row behind them.
- The MCP handshake reports the package version; it used to go out empty.

### Changed
- **Tags are normalised** (NFC + lowercase) on write and on filter, and 0015
  brings stored rows along. `X402`, `x402` and the two Unicode spellings of "й"
  were three unrelated labels — 265 distinct tags across 97 memories in the
  reference corpus, with the filter working perfectly on a vocabulary nothing
  agreed on.
- The retention sweep runs independently of the embed worker: that worker only
  exists when embeddings are configured, so riding on it would have made a
  lexical-only deployment keep expired data forever. Their shared thread and
  connection lifecycle moved to `PeriodicWorker`.
- The hash chain's optional dimensions are an append-only registry
  (`OPTIONAL_DIMENSIONS`) rather than a chain of `if`s. Order is part of the
  recipe, and a list can be asserted in a test where statement order cannot.

## [0.6.0] — 2026-08-24

### Security
- **A `user_manager` could mint itself a superadmin token.** `POST /admin/tokens`
  was gated on "may manage users", but the `user_id` in the body was arbitrary and
  token issuance never looked at the target account's role — so one request
  against a superadmin's account returned a working data-root token. Verified
  against the shipped code: with the new guard removed the call answers
  `201 Created`. Two neighbours on the same gate had the same shape: `revoke_token`
  would revoke *any* token (including the last superadmin's, locking the control
  plane out), and `list_tokens` enumerated other people's. The rule is now written
  once, in the service layer, and covers issue/revoke/list on both transports: a
  `user_manager` acts only on accounts holding the plain `user` role; anything
  touching an admin-role account requires superadmin. Not exploitable in a
  single-user deployment; live from the first account anyone provisions.

- **A weakened credential still opened the control plane.** A role says who
  someone IS; a token says what THIS credential may do. The gates consulted only
  the role, so the recommended way to hand an agent a narrow credential — a
  read-only, namespace-scoped token — protected nothing whenever the account
  behind it held an admin role: the weak token opened provisioning and issued
  itself an unscoped admin one. The data plane honoured the pin the whole time,
  which is exactly what made it look safe. Deployment-wide acts now require an
  unscoped, admin-ceiling token; per-namespace acts refuse a token scoped
  elsewhere.
- **A default namespace was an authority rather than a preference.** Resolving
  `default_namespace_id` returned `admin` on it with no reachability check, and
  the new `set_default_space` let a `user_manager` aim that pointer anywhere — so
  the tier defined as "hands out access without gaining it" could mint a
  throwaway user, point it at any namespace on the deployment, and read, write and
  irreversibly erase there with no membership row anywhere. Setting a default now
  requires that the caller administer the namespace and that the target already
  reach it; resolving one that is not reachable falls through to the
  reachable-set logic instead of granting, so a stale pointer degrades to "unset"
  rather than bricking an account.
- **Result size is bounded on the search path too.** `k` went straight into
  `LIMIT`, so one call could ask for a whole namespace and — with
  `full_body=true` — be answered with it, plus a `ts_headline` over every row.
  Search now clamps to the same ceiling browse always used.
- `memory_issue_token(space=…)` created a namespace unconditionally, walking past
  the right that governs creation everywhere else.
- Both control-plane findings were demonstrated end to end against a live
  deployment, and both fixes are pinned by tests verified to fail without them.
  Neither had been caught by the existing matrix, which only ever pointed weak
  credentials at a plain-`user` account — where the role check refuses anyway.

- **The history hash was not injective over tags.** `['a','b']` and `['a,b']`
  produced the same digest, because the tag list was joined with commas inside
  an otherwise `\x1f`-separated field list. Anyone able to write the table could
  therefore rewrite what a memory says about itself — tags are not decoration
  here, recall can be scoped to them — and `verify_history` would still call the
  chain intact. Tags now fold through the same domain-separated construction as
  title and author. Stored rows are **not** rehashed: that would rewrite the
  record whose immutability is the entire point, so each row records the recipe
  it was written with (`memory_history.hash_version`, schema v14) and is
  verified against that one. A chain spanning the upgrade verifies end to end,
  and relabelling a row to dodge the stronger recipe fails, because the two
  place the tags differently. The `source`/`reason` case was never affected.
- **`request_access` was a namespace existence oracle**, twice over. An
  unreachable namespace produced a request and a nonexistent one a foreign-key
  error, so the difference told an outsider which uuids are real — independent
  of any access they had. Making the two answers identical was the first half.
  The second: while the insert stayed *conditional*, the two cases still did
  different amounts of work, and the gap was measured at ~8× (0.13 ms against
  1.08 ms, no overlap in 40 paired trials) — the same oracle, moved into the
  clock. Two answers are only the same when the same work produces them, so the
  request is now recorded either way (`access_request.namespace_id` is no longer
  a foreign key, migration 0014) and `identity._reach` resolves reachability in
  one query instead of doing strictly less work for a namespace that does not
  exist. Re-measured after the fix: 0.99×, 23/60 paired trials. A row pointing
  at nothing is inert, and that is now enforced rather than incidental: a
  superadmin skips the membership lookup that used to establish a namespace
  exists at all, so it could *list* an orphaned request, and approving one
  failed on `namespace_member`'s foreign key — a raw driver error instead of a
  refusal, with the safety of the whole arrangement resting on a constraint in
  another table. `require_namespace_admin` now checks existence in that branch,
  which covers every per-namespace admin action at once.
  `MAX_PENDING_REQUESTS_PER_USER` (100) bounds what one account can add, and
  caps the request spam that was always possible against real namespaces; it
  bounds an account rather than an adversary, since `open` mode lets anyone
  materialize another one. Amending a request you already hold is not capped —
  it adds no row, and a caller at the limit must still be able to lower a
  pending `admin` request to `read`. 🔴 The dropped constraint carried
  `ON DELETE CASCADE`: whatever adds `delete_namespace` must delete that
  namespace's requests itself.
- **A read-only superadmin token could administer any namespace.** A role says
  who someone IS; a token says what THIS credential may do — and
  `require_namespace_admin` returned on the role before it ever looked at the
  ceiling. So the credential the docs recommend handing an agent, a read-only
  superadmin token, could rewrite any namespace's `instruction` (the routing
  hint other agents read to decide where memories land) and approve access
  requests, granting strangers write membership anywhere. Neither is a read.
  Every other caller was already held to it by `perm_min(membership, ceiling)`;
  the role was skipping past the check, not passing it.

### Removed
- **The default namespace.** `app_user.default_namespace_id` answered "where does
  an unaddressed write land", and a better mechanism already existed: a
  namespace-scoped **token** says the same thing as a property of the credential
  rather than the person — revocable, auditable, and per-credential, so one
  person can run an agent on `public` and another on `private` without the two
  fighting over a single pointer. It was also the last place anything happened
  silently, and, as the security fix above showed, it was an authority rather
  than a preference. A concept that has to be guarded is worse than one that does
  not exist. Dropping the column forgets each user's chosen default; that is the
  point, since the choice no longer means anything.
- **Implicit namespace creation.** Addressing a namespace that did not exist
  created it, so a name typed slightly wrong produced a new, empty,
  plausible-looking space and the write landed in it looking like it had worked.
  Creating one is now something you ask for — `POST /spaces`,
  `memory_create_space` — which makes a typo an error rather than a place.

### Added
- **A namespace can have a name of your own.** Name resolution now spans every
  namespace you reach rather than only the ones you own, which is what makes a
  shared namespace addressable by name at all. That admits a collision: two
  people may each own `notes`, and once one is shared with you the bare name
  means two things. It cannot be refused when a namespace is created, because the
  collision is created LATER by someone else's act of sharing — refusing then
  would let your names block other people from sharing. So it is refused when you
  address it, both candidates named, and you settle it with an **alias**: a
  private name of your own for a namespace you already reach, granting nothing.
  `POST /spaces/aliases`, `memory_set_alias`, and `list_spaces` reports it as
  what to type.
- **Adopting what `single` mode left behind.** Switching `MEMGRES_KEY_MODE` from
  `single` to open/managed stranded the entire existing corpus: everything was
  stored under one nameless namespace, no principal resolves to it afterwards,
  and every read simply came back empty — present, unharmed and invisible, with
  no signal that it had happened. `count_orphans` reports it and `adopt_orphans`
  fixes it, idempotently. It moves the chunk vectors first and the rows second,
  because a memory's namespace is written down twice and Qdrant cannot join the
  Postgres transaction: doing it the other way round and dying in between would
  leave lexical recall working and semantic recall answering *nothing*.
- **Who a user is**: `full_name`, `email`, `department`, `position`.
  `app_user.name` was doing two jobs — the handle a token resolves to and the
  thing a person reads in `blame` — and being neither unique nor required, an
  authorship line could come back as a bare uuid. History now carries the full
  name and the email. `email` is unique when present (case-insensitively),
  because it is the natural login for a web panel.
- **The substring edit answers to three names.** `old_string`/`new_string` and
  `old_str`/`new_str` are accepted alongside `replace_old`/`replace_new` and
  folded to the canon before the both-or-neither guard runs. Two spellings of one
  side carrying DIFFERENT text is refused rather than resolved — choosing one
  silently would apply an edit nobody asked for.
- **A warning when a body ends in what looks like your client's tool delimiter.**
  An LLM client emits its call in a tag-like format; a closing tag generated
  inside a value is already part of the string when it reaches us and lands in
  the memory as text. It is reported, never cleaned and never refused: bodies
  legitimately contain markup. The rule was measured on 88 live records —
  "unbalanced closing tag with one of our parameter names" alone gave two false
  positives (records *discussing* this failure); requiring it to sit at the very
  end gave none.
- **Partial reads**: `lines="40-80"` returns part of a long body. The answer is
  marked `partial`, carries `total_lines`, and withholds `content_hash` — the
  dangerous move with a slice is sending it back as a whole `body`.
- **HTTP routes for six operations the service layer already offered** —
  `set_role`, `set_default_space`, `edit_namespace` and the three directory reads
  were reachable over MCP only, which left a web panel unable to do half the
  provisioning it would show.
- **The control plane is reachable over MCP.** Twelve `memory_admin_*` tools plus
  `memory_whoami`, over the same service layer the HTTP admin routes use — so an
  operator working through an MCP client is no longer forced onto curl. Three
  operations had no door at all before this and now do: `set_role` (the
  `user_manager` role was unreachable — it could be neither granted nor taken
  away), `edit_namespace` (namespace creation is an upsert that does nothing on
  conflict, so a typo in a namespace's `instruction` could not be corrected), and
  `set_default_space`. Registration follows `MEMGRES_MCP_ADMIN_TOOLS=on|off|auto`;
  `auto` registers them wherever there are identities to administer, i.e.
  everything but `single` mode. Turning them off shortens an agent-facing tool
  list — it is a context economy, not a security boundary, since every tool
  authorizes when called.
- **`whoami` reports capabilities, not a role name** (`GET /whoami`,
  `memory_whoami`): `can_manage_users`, `can_create_namespace`, `is_admin`, the
  token's ceiling and scope. A UI that reads a role name has to re-implement the
  permission rules in its own code to decide what to show, and those copies drift.
- **Searching several namespaces at once.** `space` takes a namespace name, a
  list of names, or the keyword `"all"`; `space_id` takes ids, which remains the
  only way to name a namespace shared *with* you, since names resolve against
  your own. Over HTTP they are repeated query parameters (`?space=a&space=b`); a
  single `?space=a` means exactly what it did. Every hit, `find` row and `list`
  row now carries `space`/`space_id` saying which namespace answered.
- **Addressing a memory by its path.** A path is unique within a namespace, so it
  was always an address — but nothing accepted one, and callers who knew
  `decisions.pricing` had to search for a uuid first. `at` now takes a path
  anywhere an `id` is taken (get, write, move, forget, history, blame,
  reconstruct, verify). Over HTTP the URL segment takes either:
  `/memories/{uuid}` or `/memories/decisions.pricing`. The segment is read as an
  id when it parses as a uuid and as a path otherwise — note that modern ltree
  labels accept hyphens and non-ASCII, so `ops.rate-limits` is an ordinary path
  and any "looks like it has a dash" shortcut would misread it.
- **`at` and `path` are separate parameters doing separate jobs**: `at` FINDS the
  memory to act on, `path` SETS where a memory lives. Folded into one parameter,
  `write(path=P, body=B)` would have meant either "file a new memory at P" or
  "replace whatever is at P", distinguishable only by a flag — and the wrong
  reading silently overwrites a memory nobody meant to touch.
- **A write to an address a memory has moved away from is refused**, and the
  refusal says where it went (`if_moved`, default `error`). A caller writing to a
  stale address is working from a stale picture, and both quiet answers commit
  them to it: edit the moved memory and the write lands somewhere they did not
  name; create at the vacated path and they now hold two memories on one subject,
  the second of which they will keep writing to while the first lives on
  elsewhere — with no error at any point. `if_moved="follow"` edits it at its new
  address; `if_moved="create"` claims the vacated path for something new. Reads
  follow by default and set `moved_from`, because the moved memory is what the
  reader reached for. Deletes never follow: deleting on the strength of a stale
  address is the one mistake here that cannot be undone.
- **`write` reports `created`** — whether the call made a new memory or edited an
  existing one. That is the distinction a silent duplicate hides behind.
- **`bodies=true` on a browse** returns whole bodies instead of previews, so a
  subtree reads in one call rather than a browse plus a fetch per row. Capped in
  total by `MEMGRES_LIST_BODIES_MAX_BYTES` (default 200 KB, reported by `/info`);
  rows past the cap come back marked `body_omitted` rather than being dropped.
- **Creating a namespace is a right** (`app_user.can_create_namespace`), so an
  ordinary member can organize their own corner without also being able to
  provision people. Existing accounts are backfilled to `true` — they had the
  ability, and taking it away silently would break their writes.
- Directory reads for the control plane: `list_users` (paginated, filterable by
  role), `list_namespaces`, `list_members`, `token_owner`.
- **An MCP client is shown the tools it can actually use.** Every client used to
  see all 33: a read-only agent was offered five write tools, a plain user the
  whole control plane, and a `single`-mode deployment — which has no identities —
  advertised namespace, token and user management. The list is answered per
  request, so an http endpoint shows each client what its own token can use;
  stdio, with its pinned token, gets a constant answer. Each tool is classified
  by the capability its service function already enforces, and the capabilities
  come from `admin.capabilities()` — the same predicates that do the enforcing,
  so what is displayed cannot drift from what is allowed. **It is not an
  authorization boundary:** every tool still authorizes on call, and a client
  that calls a hidden one is refused there, with a message about permission
  rather than "unknown tool". An unclassified tool is shown rather than hidden,
  since a vanished tool is the failure nobody notices. Turn it off with
  `MEMGRES_MCP_TOOL_VISIBILITY=off`.
- **`space="*"`** — every namespace in the deployment, for a superadmin. The
  explicit counterpart to `all` no longer answering for that role (below). It
  adds no reach: the same role already opens any namespace by `space_id`, one
  call at a time. A token scoped to one namespace stays scoped.

### Changed
- **A subtree move is now recorded on every node it moves.** Moving a node
  re-addresses its whole subtree, but only the node itself got a history row:
  every descendant's path changed silently, its `seq` never advanced, and
  afterwards nothing could say where its old address had gone. Each descendant now
  gets a real `move` row on its own hash chain. Consequences, all deliberate: a
  descendant's `seq` advances and `updated_at` moves. Optimistic concurrency is
  unaffected — it is keyed on the body's content hash, which a move does not
  change — and no body is re-embedded.
- Creating at an occupied path raises `PathTaken`, naming path and occupant,
  instead of a raw unique-index violation that named neither.
- `PathMoved` and `PathTaken` are HTTP 409, not the 422 they would inherit as
  `ValueError`s: the request is well formed; what is stale is the caller's picture
  of where things live.
- The control plane moved into a service layer (`memgres/admin.py`). Both
  transports are now adapters over it and contain no permission logic of their
  own — which is what let the escalation above be fixed in one place rather than
  two. HTTP keeps mapping domain errors to status codes; MCP gained that mapping
  for free, having previously handed clients raw Postgres error text.
- `admin.py` takes a `Principal`, never a token: authentication belongs to the
  transport, authorization to the service. A future web login is then one more
  authenticator producing the same `Principal`, with no change to the core.
- **`whoami` reports what THIS credential may do, not what the role could.** A
  superadmin holding a scoped or read-only token cannot provision with it, and
  the old answer said it could — sending a caller, or a UI rendering itself from
  the answer, at doors that refuse them. Two new keys separate the halves of
  "admin" that are actually independent: `has_admin_ceiling` (this credential)
  and `can_administer_deployment` (the role *and* an unscoped admin credential).
  `can_write` and `can_manage_own_tokens` join them; `is_admin` stays the plain
  role fact, since per-namespace authorization consults the role.

### Breaking
- **Reaching several namespaces and naming none is an error**, for reads and
  writes alike — there is no default to fall back on any more. Exactly one
  reachable namespace still resolves on its own, since there is nothing to
  choose. The error names the candidates and, for a search, the `all` keyword.
- **A user with no namespace and no right to create one is refused**, with an
  explanation, instead of silently getting a second namespace called `default`.
  This is the provisioning bug that made every freshly-provisioned account
  unusable: `create_user` + `create_namespace` never set `default_namespace_id`,
  so the account's first read failed and its first write quietly created a
  namespace nobody had asked for. In `open` mode, where accounts materialize
  themselves and there is no admin to ask, the right is granted on creation.
- **An admin-ceiling token is no longer accepted unscoped** on either transport,
  symmetrically. (The idea of making MCP stricter than HTTP was dropped: it was
  argued from a private-network topology that is our deployment, not a rule.)
- Recall hits, `find` rows and `list` rows carry two new keys (`space`,
  `space_id`); `list` rows in `bodies` mode carry `body`/`body_omitted` in place
  of `preview`. Anything asserting an exact key set will notice.
- Redirect resolution only knows about moves recorded from this version on.
  Subtrees moved by an earlier version left no trail — the bulk update wrote none
  — and it cannot be reconstructed: after several moves the current shape of the
  tree does not determine the addresses a node held before.

- **A namespace cannot be created with a name one of the owner's aliases already
  claims.** The rule existed on the self-service door and was missing from the
  two admin-side ones, so an admin provisioning you a `private` namespace while
  you had an alias of that name left every `space="private"` write landing in the
  *aliased* space — which someone else can read — with a 200 and no warning. It
  now lives at the single point all three funnel through.
- **A read-only token cannot create a namespace**, and one account cannot own an
  unbounded number of them. In `open` mode a never-registered token materializes
  its own account, so the new `POST /spaces` was otherwise a free INSERT loop for
  anyone able to generate tokens.
- **A partial read reports the contiguous runs it actually returned**, not first
  and last: `lines=1,5` reporting `[1, 5]` reads as "one through five". A
  selection matching nothing is an error rather than an empty body wearing
  `partial: true`.
- `email` is unique but **not verified**: anyone who may provision users can set
  any address on a plain-user account, so an address can be claimed before its
  owner has one. Harmless while email is a label; whatever adds email login must
  add ownership verification in the same change.
- **`space="all"` is refused for a superadmin** when it would answer with less
  than that credential can read — that is, when namespaces exist outside its
  memberships. For every other caller `all` is unchanged, because for them it
  genuinely is everything. The refusal names what `all` would have covered and
  offers `space="*"`. Widening the word instead would have pulled other tenants
  into a routine agent search; leaving it was the silent partial answer this
  project weighs as heavily as a leak. There is deliberately no second word for
  "the ones I belong to": namespace names are free text and the obvious
  candidates are names people use — `mine` shadowed a namespace in this repo's
  own tests on the first attempt.
- **`POST /spaces/{id}/access-requests` answers `202` with `{"status": …}`**,
  not `201` with the request id. The id is gone because the requester has no use
  for it — deciding belongs to whoever administers the namespace, who reads ids
  from `list_requests` — and because returning one is half of what made the
  route an existence oracle. `already_reachable` is still reported: that is the
  caller's own access, which `list_spaces` shows them anyway.
- **`whoami` capabilities gained keys and changed meaning** (see Changed).
  Anything asserting the exact three-key dict will notice. `can_create_namespace`
  now mirrors every condition `create_own_namespace` enforces — a read-only or
  scoped credential, or one with no owning user, reports `false` — rather than
  the bare right, which advertised a door that always closed.
- **A superadmin read that names no namespace at all is refused too**, on the
  same terms as `all`: with one membership and other namespaces on the
  deployment, "your only namespace" answers a narrower question than was asked.
  The write path is deliberately unchanged — a write has to land somewhere, the
  namespace you belong to is the only sane target, and nothing is left out of an
  answer.
- **The compatibility floor moves to schema v14**, so a client older than this
  release refuses to run against a database this release has touched. Two
  reasons, and the second is why it is stated as a floor rather than a note:
  0011 dropped `app_user.default_namespace_id`, which every 0.5.x read path
  selects; and 0013 changed what a stored `row_hash` means, so a pre-0013 client
  recomputes v2 rows with the v1 recipe and reports an untampered chain as
  **tampered** — a silent wrong answer from the one function whose whole job is
  to be trusted. **Upgrade every client of a shared database together.**
  A version this build does not recognise is now an error naming the version,
  not a "tampered" verdict: an unknown recipe means the row is newer, not bad.
- `identity.request_access` (exported) returns the request id as `str` rather
  than `Optional[str]`, and raises `ValueError` at the per-account cap. The
  None-for-a-missing-namespace case is gone — there is no missing case any more.

### Known, deliberately not changed here
- `email` is unique but **not verified** — see the note under Breaking. Whatever
  adds email login must add ownership verification in the same change.

## [0.5.2] — 2026-08-24

### Fixed
- **Container healthcheck is role-aware.** The image's `HEALTHCHECK` probed a
  hardcoded `localhost:8080/healthz` — the REST server's endpoint. An MCP
  container (`memgres-mcp`, `/mcp` on `MEMGRES_MCP_PORT`) never listens there, so
  it reported *unhealthy* forever while serving normally; a permanently-red check
  masks real failures and blocks anything waiting on `service_healthy`. The probe
  now lives in `memgres/healthcheck.py` (entry point `memgres-healthcheck`) and
  picks its target from PID 1's argv, not an env convention: `/healthz` on
  `MEMGRES_HTTP_PORT` for REST, `/mcp` on `MEMGRES_MCP_PORT` for MCP over HTTP,
  and an unconditional pass for MCP over stdio (no socket exists to probe). Two
  details it now gets right: it uses `127.0.0.1` rather than `localhost` (a
  server bound to `0.0.0.0` is IPv4-only, while `localhost` may resolve to `::1`
  first), and it treats *any* HTTP answer from the MCP port as healthy, since
  Streamable HTTP rejects a bare GET with 400 — a response that still proves the
  port is bound. This also fixes REST deployments that remap `MEMGRES_HTTP_PORT`,
  which the fixed-8080 probe failed.

## [0.5.1] — 2026-08-21

### Fixed
- **HTTP MCP transport now binds under mcp SDK 2.x.** `memgres-mcp` with
  `MEMGRES_MCP_TRANSPORT=http` set `server.settings.host/port`, which the 2.x
  `MCPServer` rejects (`"Settings" object has no field "host"`) — the container
  crash-looped and never served on its port (bootstrap still ran). `main()` now
  passes host/port as `run()` kwargs on 2.x and keeps the `settings` path for
  1.x. Added a regression test that spawns the transport and asserts it answers.

## [0.5.0] — 2026-08-21

### Added
- **Service roles + first-admin bootstrap (managed mode).** Users now carry a
  service role (`user` | `user_manager` | `superadmin`) via `app_user.role`,
  orthogonal to per-namespace membership. `Principal.is_admin` derives from the
  superadmin role, so admin actions attribute to a real user instead of an
  anonymous env root. A managed server seeds its first admin once at startup from
  `MEMGRES_ADMIN_TOKEN` or `MEMGRES_ADMIN_TOKEN_FILE` (read-or-create,
  Jenkins-style) — only when zero admins exist; inert thereafter. The seed role is
  `MEMGRES_ADMIN_ROLE` (default `user_manager`). Schema v9 (additive).
- **Role-gated REST provisioning + `grant`/`revoke-superadmin` endpoints.**
  `/admin/*` is gated by the caller's role: user/token management needs
  `user_manager`+, cross-tenant member-add and role grants need `superadmin`. A
  `user_manager` cannot mint an admin-role user.
- **`memgres-grant-superadmin` CLI** — promotes a user directly over the database
  (Django `createsuperuser` analog); the break-glass path and lockout recovery.

## [0.4.1] — 2026-08-19

### Fixed
- **`replace_old` without `replace_new` no longer silently deletes the matched
  text.** Both the `memory_write` MCP tool and `PATCH /memories/{id}` coerced a
  missing `replace_new` to `""`, so a lone `replace_old` (a client that dropped
  the field, or a parameter-name typo) rewrote the match to nothing and returned
  success — a silent edit-into-delete on durable memory. A shared `build_replace`
  helper now rejects exactly one of the pair with a `ValueError` (HTTP 422),
  while an *explicit* `replace_new=""` still deletes on purpose.

## [0.4.0] — 2026-08-19

### Changed
- **Chunks are the semantic index; embedding moved off the write path.** A memory
  is now indexed as its overlapping chunks, not one whole-body vector. Recall
  ranks over the chunk vectors and keeps the **best chunk per memory** (one hit
  per memory, whose winning chunk is also its snippet), so a match in the tail of
  a long body is found (a single vector couldn't represent 60 KB) and one long
  document can't crowd distinct memories out of the top-k (an iterative-exclude
  loop, round-capped and logged, dedups by memory). A write no longer embeds
  inline on the server: it flags the row and a background worker segments, embeds,
  and indexes it — so a write returns fast regardless of body size. Dispatch is
  chosen by `MEMGRES_EMBED_DISPATCH` (`inline`|`async`) with the worker settings
  below (see *Added → Split deployment*). Embedded/library use (no worker) stays
  `inline` by default, so semantic recall is correct the instant a write commits. **Upgrade note (schema v6):** the old `memory.embedding`
  column is dropped and every existing row is flagged once for re-chunking — the
  worker rebuilds the index from the bodies on first run; bodies and history are
  untouched, no manual reindex. A qdrant deployment can drop its old
  `{collection}` doc-vector collection (chunks live in `{collection}_segments`).
- **Recall returns one body view per hit — never both a slice and the whole
  body.** Each hit now carries `snippet` plus `kind` (`"snippet"` | `"full"`) and
  `lines` (`[start, end]`, 1-based inclusive), replacing the old separate `body`
  field and scalar `line`. A hit gets a **slice** (`kind="snippet"`) when its body
  is long; semantic/hybrid pick the best-matching segment with an exact `lines`
  range, lexical uses `ts_headline`. A body short enough that a slice would just
  repeat it comes back **whole** (`kind="full"`, `lines=[1,N]`) — the threshold is
  the new `MEMGRES_FULL_BODY_MAX_CHARS` (default 500). `MEMGRES_FULL_BODY` now
  **defaults to `false`** (was `true`): pass `full_body=true` (per call or via env)
  to force whole bodies, `snippet=false` to skip slicing. This trims recall
  responses so an agent isn't handed a slice *and* a wall of text for every hit.
- **Lexical/fallback snippets are now clean prose** — `ts_headline` runs with
  empty `StartSel`/`StopSel`, so the returned text has no `<b>…</b>` markup that
  could mislead a model reading it.

### Added
- **Split (enterprise) deployment topology.** For many clients, run a stateless
  API tier that only *flags* writes (`MEMGRES_EMBED_DISPATCH=async` +
  `MEMGRES_EMBED_WORKER=off`) plus a scalable `memgres-worker` tier that embeds.
  Draining is **claim-based** (`FOR UPDATE SKIP LOCKED`), so worker replicas never
  embed a memory twice or block each other, and a crash mid-embed leaves the row
  flagged for retry — never stuck (the claim lock releases when the connection
  dies). A row that keeps failing to embed is retried with back-off and, after
  `MEMGRES_EMBED_MAX_ATTEMPTS`, dead-lettered (out of rotation, logged) so one
  poison body can't wedge the queue behind it. `MEMGRES_EMBED_DISPATCH`
  (`inline`|`async`) replaces the derived async flag; `inline` stays the safe
  library/all-in-one default. New `memgres-worker` entrypoint,
  `deploy/docker-compose.yml`, and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
- **`docs/AGENT_MEMORY_GUIDE.md`** — a playbook for using memgres as an agent's
  long-term org memory: an example `MEMGRES_INSTRUCTION` (the ≤2 KB startup rulebook)
  plus the full reasoning mapped to memgres features — two-layer raw/distilled split,
  atomic write discipline, ADR-shaped decisions with rationale, source+date trust,
  conflict resolution ("memory says X, someone says Y"), and hygiene. Notes the tool
  gaps (dedup-at-write, freshness fields, first-class links) that instruction covers
  for now.
- **`docs/CHOOSING.md`** — a decision guide for picking a run mode, with the
  explicit recommendation that **more than one user should run the shared Docker
  server, not a per-machine install** (one pool, one worker, central tokens, one
  thing to upgrade and back up). Linked from the README and `docs/DEPLOYMENT.md`.
- **`memgres-reembed`** — switch the embedding model/dimension on an existing
  store: re-stamps the model, wipes and recreates the chunk index at the new
  dimension, flags every memory, and rebuilds — bodies and history untouched.
  (Normal startup still refuses a silent model change.)
- **`MEMGRES_CHUNK_CHARS` / `_OVERLAP`** — clearer names for the chunk index size
  and overlap (the legacy `MEMGRES_SNIPPET_SEG_*` still work).
- **Compatibility floor + startup version guard.** The meta row now carries
  `min_reader_version` alongside `schema_version` — the low end of the range of
  client `SCHEMA_VERSION`s allowed to operate against the data (the high end is
  open: a newer client migrates forward). It is raised only by a
  backward-incompatible migration (tracked by `SCHEMA_BREAKING_VERSION` in code),
  so an older client keeps working against a newer-but-additive schema. On
  connect, a client whose `SCHEMA_VERSION` is below the database's floor refuses
  to run with an actionable "update this client" message instead of silently
  misreading it — which is exactly what a stale machine would otherwise hit after
  another machine upgrades the shared store past a breaking change. A fresh or
  in-range database migrates forward as usual, and the stamp is monotonic
  (operating with an older client never downgrades a newer database's versions).
- **Server-side MCP `instructions`** — set `MEMGRES_INSTRUCTION` and the text is
  emitted in the MCP `initialize` response, so a client that honors it (e.g.
  Claude Code) loads it once at connect to guide how the model uses the memory
  (without inflating every tool response). Optional — unset, the field is omitted
  entirely — and byte-capped (2 KB, on a UTF-8 boundary) to stay small.
- **Curated `title` + `memory_find`** — a memory can carry a short, human-curated
  `title` (set whole, distinct from the body's first-line preview), returned on
  `get`/`list`/recall hits. `memory_find` (MCP) / `GET /find` locate memories by
  their **title + tags** only — light rows `{id, path, title, tags, score}`, never
  the body, no vectors — a cheap "where is it?" scan before a heavier recall (works
  without an embedder). Title changes are audited in the hash-chained history
  (`title_before`/`title_after`, op `retitle`) and folded into the chain **only
  when the title actually changes**, the same domain-separated way as author — so
  every pre-title row keeps its exact digest and still verifies. New
  `MEMGRES_MAX_TITLE_BYTES` (default 256), reported in `server_info`.
- **Substring edit (`replace`)** — edit a memory by sending `replace_old` →
  `replace_new` instead of hand-building a unified diff: the server finds
  `replace_old` in the current body and rewrites just it. `replace_old` must be
  unique unless `replace_all=true` (else a clear error asks for more context);
  a missing `replace_old` or a no-op (old == new) is rejected, never a silent
  write. Because only `old`+`new` cross the wire (size-capped), a body larger
  than `MEMGRES_MAX_WRITE_BYTES` stays editable — which a whole-body rewrite
  can't do. It lowers to the existing diff+OCC path, so history stays a single
  replayable, line-attributable chain (`base_hash` optional here; supplied adds
  strict OCC). On the `memory_write` MCP tool and `PATCH /memories/{id}`.
- **`server_info` now reports `version` and `schema_version`** — a client can tell
  which memgres it's talking to (and which DB layout) without guessing. The
  version is read from code (`memgres.__version__`), so an editable/dev checkout
  reports what it's actually running, not stale install metadata. Exposed on both
  the `memory_server_info` MCP tool and `GET /info`.
- **Authoritative authorship in history** — every `memory_history` row now records
  the server-resolved principal (`author_user_id` + `author_token_id`) on each
  write, separate from the free-text `source`/`reason` a client supplies. In a
  shared namespace this answers *who* actually made an edit, not just who claims
  to have. `history`, `blame` (per-line + grouped), the `memory_history` /
  `memory_blame` MCP tools and the HTTP `…/history` / `…/blame` endpoints expose
  it, resolving `author_name` from the user row via LEFT JOIN (a since-deleted
  author reads back as its bare id). The author is folded into the tamper-evident
  hash chain, so stripping or swapping authorship is detectable by
  `verify_history`.

### Security
- **`replace_all` can no longer amplify a write into an out-of-memory.** A
  substring `replace_all` multiplies `new` by every occurrence of `old`; the
  result is now bounded against `MEMGRES_MAX_BODY_BYTES` **before** the string is
  materialized (projected from the occurrence count), instead of only after —
  closing a path where one authenticated write could allocate gigabytes.
- **History hash fold is injective over its fields.** Each dimension folded into
  the tamper-evident chain (author, title) now reduces every field to its own
  fixed-width hash before joining, so a `\x1f` inside a client-supplied field
  (e.g. a crafted title) can't shift a field boundary to collide two logically
  different rows. (Impact was already negligible — the chain is unkeyed — but the
  claimed property now actually holds.)

### Notes
- Backward compatible: user-less writes (single mode, and the global-admin env
  token) stamp NULL author and hash **exactly** as before, so history chains
  written before this release still verify. New columns are added by an
  idempotent migration (schema v4); no reindex or downtime.
- No foreign key ties the author columns to `app_user`/`token`: the history is an
  immutable audit record, so deleting a user must not mutate (and break the
  verifiability of) unrelated memories' chains. A dedicated author-purge is a
  future admin op.

## [0.3.2] — 2026-07-31

### Fixed
- **Silent no-op on a malformed diff.** `apply_diff` skipped any line that wasn't
  a valid `@@ -a,b +c,d @@` header, so a patch with a malformed hunk header (or
  none at all) applied nothing and returned the body unchanged — while the write
  still bumped `seq`/`updated_at`, looking like success. It now raises
  `DiffConflict` (HTTP 409) instead, and a `diff` write that leaves the body
  identical is likewise rejected. Empty patches remain a legitimate no-op.

## [0.3.1] — 2026-07-31

### Fixed
- Compatibility with the **mcp SDK 2.0**, which removed the `mcp.server.fastmcp`
  module (renamed to `mcp.server.mcpserver`). The unguarded `Context` import
  broke `memgres.mcp_server` on mcp ≥ 2.0 — now imported from either path.
  memgres works on mcp 1.x and 2.x. (0.3.0 shipped with this import bug.)

## [0.3.0] — 2026-07-31

### Added
- **Recall snippets** — each hit now carries a `snippet` (the most relevant slice
  of its body) plus a `line` number. Semantic/hybrid hits pick their
  best-matching *segment*, lazily embedded and cached per body-hash (recomputed
  when the body changes); lexical/hybrid fall back to Postgres `ts_headline`.
  Tunable via `MEMGRES_SNIPPET*` settings plus per-call `snippet` / `full_body`
  params on `memory_recall` and `GET /recall` — pass `full_body=false` for just
  the snippet.
- **Pluggable vector backend** (`memgres/vector/`): pgvector and Qdrant now sit
  behind one `VectorBackend` interface, so the store and search never branch on
  which one is configured and a new backend is a single module. Internal
  refactor — no behavior change.
- **`memory_list`** — browse/enumerate a subtree *without* a query (not a search:
  no full-text, no vectors), returning each memory's path, tags, a first-line
  preview, and timestamps. Available as the `memory_list` MCP tool and
  `GET /memories`. Preview length via `MEMGRES_LIST_PREVIEW_CHARS` (default 120).
- **`server_info`** — read-only introspection of the effective configuration
  (limits, embedding provider/model/dimension, available recall modes, vector
  backend, key mode). Carries no secrets. Available as the `memory_server_info`
  MCP tool and `GET /info`.
- **Configurable lexical match** — `MEMGRES_LEXICAL_MATCH` (`any` | `all`) plus a
  per-query `match` override on recall.
- **Provenance size caps** — `MEMGRES_MAX_SOURCE_BYTES` (default 2048) and
  `MEMGRES_MAX_REASON_BYTES` (default 1024); a write whose `source`/`reason`
  exceeds the cap is rejected, alongside the existing body/write ceilings.
- **`MEMGRES_EMBED_MAX_SEQ`** — override the local model's max sequence length, a
  guard against silently truncating long inputs when a model's default window is
  small.

### Changed
- **Lexical recall now defaults to OR (`any`)** — a query's words are OR-ed and
  ranked, so recall returns ranked partial matches instead of nothing when not
  every word is present. Set `MEMGRES_LEXICAL_MATCH=all` (or pass `match="all"`)
  for the previous AND-narrowing behavior.
- With no embedder (`MEMGRES_EMBED_PROVIDER=none`), the `memory_recall` MCP tool
  no longer advertises `semantic`/`hybrid` modes — the model only sees what works.
- pgvector writes the embedding via a separate `UPDATE` within the same write
  transaction (was inline in the INSERT). No visible behavior change.

### Security
- Segment-cache reads (`get_segments`) now filter by `namespace` in addition to
  `memory_id`, so a tenant's snippet cache is *structurally* scoped rather than
  relying on memory-id unguessability. Defense-in-depth — not a fixed exploit
  (the id was already sourced from a namespace-scoped recall). Verified by an
  adversarial cross-tenant test (`test_qdrant_two_namespaces_isolated`).
- `GET /info` / `memory_server_info` are unauthenticated by design and return no
  secrets (config metadata only) — documented so `managed` deployments can gate
  it at the proxy if even that must stay private.

### Notes
- With a **paid** embedding API, semantic snippets add model calls on first sight
  of each hit (segments are embedded, then cached). Set
  `MEMGRES_SNIPPET_SEMANTIC=false` to disable them and use `ts_headline` instead.
  With a local model the cost is negligible (CPU/GPU only).

### Documentation
- New `docs/EMBEDDINGS.md` — choosing and operating a local vs cloud embedding
  model, with the operational gotchas (dimension drift, context-window
  truncation, offline caches).

## [0.2.1]

### Added
- `MEMGRES_QDRANT_CA` — trust a self-signed / private-CA `https` Qdrant by
  pointing at its PEM certificate.
- Keyword payload index on `namespace` in the Qdrant backend, keeping
  tenant-filtered vector search fast as the collection grows.

## [0.2.0]

### Added
- **Identity & multi-tenancy** — users own namespaces; rotatable,
  permission-scoped `mgk_` tokens; `MEMGRES_KEY_MODE` = `single` | `open` |
  `managed`; admin provisioning and request-access over HTTP; the MCP identity is
  pinned in the client config so the model never handles tokens.
- Connection pool sized by `MEMGRES_POOL_SIZE` (HTTP + Streamable-HTTP MCP).

### Changed
- Removed the legacy `MEMGRES_NAMESPACES`; `MEMGRES_KEY_MODE` is the only tenancy
  mechanism.

### Security
- Closed a cross-tenant read and an MCP token-management privilege escalation
  (with adversarial isolation tests); constant-time admin-token comparison.

## [0.1.0]

Initial release: versioned document memory on one Postgres — whole-body or
unified-diff writes with content-hash optimistic concurrency, a hash-chained,
deletable history with git-like blame and reconstruct, an `ltree` tree plus tags,
and lexical (Postgres FTS) / semantic (pgvector or Qdrant) / hybrid (RRF) recall
behind pluggable embedding providers. HTTP (FastAPI) and MCP (stdio + Streamable
HTTP) layers. Published to PyPI and GHCR.
