# Using memgres as an agent's long-term org memory

This is a **playbook for an AI agent** that uses memgres as durable, cross-session
memory for an organization — accumulating facts about the company (structure,
processes, configuration, products) and the **decisions** behind them, so it can
answer questions reliably over time.

It has two parts:
1. **The startup instruction** you give the agent (`MEMGRES_INSTRUCTION`) — short,
   always-on rules loaded at connect.
2. **The full playbook** — the reasoning behind those rules, mapped to memgres
   features, for humans configuring the deployment.

The hard problem is not *storing* knowledge but keeping it **trusted and current**.
Knowledge bases don't fail from missing data — they fail when stale, duplicated, or
conflicting entries pile up and people stop trusting them. Every rule below serves
that: only current, attributed facts; one home per fact; record *why*; never
silently overwrite.

---

## 1. The startup instruction (`MEMGRES_INSTRUCTION`)

memgres emits a server-side `instructions` string in the MCP `initialize` response;
a client that honors it (e.g. Claude Code) loads it **once at connect**, so the
model follows it without you re-stating it every turn. Set it via the
`MEMGRES_INSTRUCTION` environment variable on the server/MCP process.

**It is capped at 2048 bytes** (truncated on a UTF-8 boundary). That is deliberately
small — it is the always-apply rulebook, not the manual. Keep the full guidance in a
doc like this one and distill the essentials into the 2 KB. A ready example (English,
~1.5 KB — adjust to your org and language; note non-ASCII costs 2 bytes/char against
the cap):

```
memgres is your durable cross-session memory. Prefer it over chat history.

RECALL FIRST: before any non-trivial step, memory_recall the topic. Treat hits as
hints, not gospel — check each fact's date; if it's old and the area changes,
re-verify against live data instead of repeating it.

WRITE DISCIPLINE:
- One fact per memory. Give it a title, a path (tree position), and tags. Keep
  bodies small; link related notes with [[path]] instead of restating them.
- Always set source — never store a fact you can't attribute.
- Before creating, memory_recall first: if it already exists, EDIT it
  (replace_old->replace_new, or a diff), don't duplicate. One fact, one home.

DECISIONS: store under decisions.* with What / Context / Why / Alternatives-rejected
/ Consequences / Status. Record the WHY, not just the what. To change a decision,
write a new record and mark the old one superseded — don't erase it.

CONFLICTS (memory says X, someone says Y):
1. New state (was true, changed) or correction (was always wrong)? New -> write the
   new value, old stays in history. Correction -> fix it, with a reason.
2. Who wins: authoritative source > confirmed by several > newer > more confident.
   A fresh human claim is a CANDIDATE to verify, not automatic truth — people are
   wrong, stale, or mistaken too.
3. High stakes or equal authority -> don't overwrite: tag it disputed, keep the old
   value live, escalate to a human.
4. Found wrong info? Fix it at the source memory; don't just record the truth
   elsewhere and leave the wrong entry to mislead again.

HYGIENE: keep only current, attributed facts. Supersede, don't pile up. Use forget
only for real erasure (GDPR / explicit deletion).
```

For multi-tenant deployments, each namespace also has its own `instruction` field
(set via `edit_namespace`, returned by `list_spaces`) — use it as a **routing hint**:
"this space is for X, write Y here," so an agent picks the right space before writing.

---

## 2. The full playbook, mapped to memgres features

### 2.1 Two layers: raw material vs. distilled knowledge

Keep the **transcripts/logs you learn from** separate from the **facts you rely on**.
Use the tree (`path`) to model this:

- `sessions.*` / `raw.*` — episodic: what happened, raw notes, source material. The
  agent reads these to extract from, but does **not** answer directly out of them.
- `org.*`, `ops.*`, `products.*`, `decisions.*` — distilled: the timeless facts and
  decisions the agent answers with.

Extract facts from the raw layer into the distilled layer; don't let the agent
retrieve from the transcript firehose.

### 2.2 Write discipline (keeps it from becoming a wall of text)

- **One idea per memory.** Small body, a curated `title`, a `path`, and `tags`.
  The title is required for a write that stores content (`MEMGRES_REQUIRE_TITLE`):
  it names the memory in a result list, and recall weighs a match there higher
  than one in the body. Atomic notes are what make dedup, linking, and targeted
  edits possible — you can't cleanly update a fact buried in a 60 KB page.
- **Attribute everything.** Set `source` on every write. A fact you can't trace you
  can't verify or safely supersede.
- **Link, don't restate.** Write `[[path]]` in the body (or
  `[[path#anchor|label]]`) instead of repeating what another memory says. The links
  become a real graph: `memory_links` walks it in both directions, and the INBOUND
  half is the one that matters — before changing a fact, it tells you who is relying
  on it. A link to something not written yet is fine and deliberate: it stands as
  `resolved: false` and binds itself when the target appears.
- **Reuse the tag vocabulary.** `memory_tags` lists what is already in use;
  a label you invent that exists in another wording becomes a second, unrelated
  tag. Case and Unicode form are normalised for you — wording is not.
- **Dedup before you write.** `memory_recall` the topic (`bodies=false` for a cheap scan)
  first; if it exists, **edit that memory** (`replace_old`→`replace_new` for a
  surgical change, or a `diff`) instead of creating a second copy. *One fact, one
  home.* (Automatic semantic dedup-at-write is not yet enforced by the tool — this is
  a convention today; see Gaps.)
- **Link, don't restate.** When new material expands an existing note, fold it in and
  reference related notes (informal `[[path]]` today) rather than spawning a parallel
  entry.

### 2.3 Recording decisions with rationale

Store decisions under `decisions.*`, one decision per memory, with a body shaped like:

```
What:                 the decision, in one line
Context:              the situation / forces that prompted it
Why:                  the rationale — why THIS choice
Alternatives rejected: what else was considered, and why not
Consequences:         what it implies / trade-offs
Status:               accepted | superseded | deprecated
```

Give it a short `title` (the decision's name) and tag it with the entities/products
it affects. **The "why" is the highest-value, most-easily-lost knowledge** — "what"
without "why" is useless once circumstances change and nobody can tell if the
decision still applies. To change a decision, write a **new** record that references
the old and set the old one's `Status: superseded` — the hash-chained history keeps
the full trail; don't overwrite.

### 2.4 Sources and dates (trust)

- **Source:** the `source` field, on every write.
- **Dates:** memgres stamps `created_at` / `updated_at` automatically, and `recall`
  returns them — so the agent can (and must) **check a fact's age before treating it
  as current**. A fact that was true once ("we're on Postgres") can be stale ("…six
  weeks after migrating to MySQL").
- **`valid_at` is the one that actually answers "is this still true?"** The stamped
  dates say when a row was WRITTEN, which is a different question: fixing a typo
  moves `updated_at` without anyone having re-checked the content, and a fact
  distilled today from a letter dated 2021 is not fresh because the row is new.
  `valid_at` (YYYY-MM-DD, on the history row) is the day the content was last known
  to be ACCURATE.
  - Set it when the fact comes from a dated source, or when you have just
    re-checked one. Omit it and it means "accurate as of now" — the ordinary case.
  - It may point into the past. That is not a mistake and nothing enforces order.
  - Sending ONLY `valid_at` records a re-confirmation (`op: revalidate`) without
    touching the body — so "I checked, still true" is a first-class entry in the
    history rather than a fake edit.
- A review-by field and a staleness sweep are still not first-class (see Gaps).

### 2.5 Conflict resolution (memory says X, someone says Y)

Don't blindly trust either side. Decide in this order:

1. **New state or correction?** If the world *changed* (job, price, config), the old
   fact wasn't wrong — write the new value and let the old one live in history
   ("until date D it was X, now Y"). If the memory was *always* wrong, fix it with a
   `reason` recording the correction.
2. **Who wins** (fall through on ties): authoritative source *for this fact* >
   confirmed by several independent sources > newer > more complete/confident. A
   corroborated older fact can rightly beat a lone newer claim.
3. **A fresh human statement is a candidate to verify, not automatic truth.** Verify
   it: check the system of record, ask the owner of the fact, look for corroboration,
   check whether the speaker has authority over it.
4. **High stakes / equal authority → don't overwrite.** Tag the entry `disputed`,
   keep the old value live, and escalate to a human; don't deadlock it stale forever.
5. **Fix wrong info at its source memory** — editing the entry that was wrong — not by
   recording the truth in a different note and leaving the wrong one to mislead again.

Rule of thumb: "newer wins" is right for fast-changing, owner-asserted facts (status,
price, preference) and **wrong** for stale/low-authority new claims, for facts that
merely belong to a different time period, and for anything irreversible. Decide
freshness by **dates/versions**, not by asking the model to judge which is newer.

### 2.6 Hygiene (so it stays current, not a graveyard)

- **Supersede, don't pile up.** Editing updates in place and keeps history; the store
  doesn't grow a new row per restatement.
- **`forget` only for real erasure** (GDPR / explicit deletion) — it's destructive
  and loses provenance. For "no longer true," mark superseded instead.
- **Review by risk × usage.** High-traffic / high-blast-radius facts get checked more
  often. Use `memory_list` to browse a subtree and spot stale or duplicated entries.
- **Never confidently serve unverified/stale facts.** One wrong answer erodes trust in
  the whole memory — "I don't know / unverified" is the safer output.

---

## 3. Known gaps (tool vs. convention)

Granularity (atomic vs. prose) is enforced by **instruction** today; a few supports
that make the discipline *stick* are **tool** features still on the roadmap:

- **Semantic dedup-at-write** — flag/merge a near-duplicate on write instead of
  relying on the agent to `find` first.
- **Staleness sweep** — `valid_at` records how far forward the evidence reaches
  (§2.4), but nothing yet SURFACES what has gone quiet: no review-by, no "show me
  facts whose evidence is older than N months". Recording is in, retrieval is not.
- **Anchors are a hint, not a contract.** `[[path#anchor]]` is recorded, but
  nothing yet resolves it to a place inside the body — a link lands on the whole
  memory. When you need to point at part of one, that is usually a sign the target
  should be split.

Until those land, the instruction in §1 carries the load; the tool supports the rest
(tree, tags with a shared vocabulary, required titles, recall over titles and
bodies, the link graph with backlinks, replace, hash-chained history with
`valid_at`, retention, forget).
