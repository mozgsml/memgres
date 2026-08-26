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

**It is capped at 4096 bytes** (truncated on a UTF-8 boundary), and that ceiling is a
guardrail rather than a target. It is the always-apply rulebook, not the manual: it
sits in the system prompt and is re-read every turn, so a long one is followed less
reliably than a short one. Keep the always-true rules inline — what must never be
stored, what must accompany every write — and point at a memory (`[[path]]`) for
anything situational, such as how to resolve a conflict. The store you are telling
the agent about is the right place for depth.

Note the cap is in BYTES: non-Latin text costs two bytes per character, so the same
rulebook in Russian fits half as much. A ready example (English, ~1.5 KB — adjust to
your org and language):

```
memgres is your durable cross-session memory. Prefer it over chat history.

RECALL FIRST: before any non-trivial step, memory_recall the topic. Treat hits as
hints, not gospel — check each fact's date; if it's old and the area changes,
re-verify against live data instead of repeating it.

SOURCES ARE INPUT, NOT MEMORY: emails, threads, logs, dumps, tool output, page text
— never store them, not even trimmed or summarised. Read the source, extract the
durable claims, write those. One thread may yield several small memories, or none —
"nothing durable here" is a valid outcome.

WRITE DISCIPLINE:
- One fact per memory. Give it a title, a path (tree position), and tags. Keep
  bodies small; link related notes with [[path]] instead of restating them.
- Always set source, and make it an ADDRESS someone else can follow back to the
  original: host + absolute path; mailbox, sender -> recipient, date, subject;
  messenger, who with whom, date; machine + project + session for an agent run;
  full URL + date read. "email", "the meeting", "the user said" is not a source.
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

### 2.1 Sources are input, not memory

Emails, chat threads, logs, dumps, tool output, page text — **never store them**, not
even trimmed or summarised verbatim. Read the source, extract the durable claims,
write those; the original stays where it already lives and the memory carries its
address in `source` (§2.2).

An earlier version of this guide suggested a `sessions.*` / `raw.*` branch to hold
raw material "to extract from later". That was a mistake worth naming, because it is
the mistake everyone makes: **give raw material an address and it will be filed
there.** A store that has somewhere to put transcripts fills up with transcripts, and
then search returns fragments of conversations instead of answers — at which point
nobody trusts it, which is the only failure a memory cannot recover from.

Two things make the rule hold in practice:

- **Say it as an action, not a prohibition.** "Read it, extract the claims, write
  those" tells an agent what to do; "don't store raw material" leaves it holding a
  thread it feels obliged to file somewhere.
- **Allow the empty result.** One thread may yield several small memories, or none.
  "Nothing durable here" is a valid outcome, and saying so explicitly is what stops
  an agent from writing a summary purely because it read something long. That urge
  — having read 200 messages, surely *something* must be recorded — is how raw
  material gets in past every other rule.

A memory earns its place if it can be read a year later, out of context, and answers
a question of the shape "how does X work here", "what does Y cost", "why did we
decide Z". "Ivanov wrote that…", "discussed the deadline", "correspondence about the
contract" describe events, not knowledge.

### 2.2 Write discipline (keeps it from becoming a wall of text)

- **One idea per memory.** Small body, a curated `title`, a `path`, and `tags`.
  The title is required for a write that stores content (`MEMGRES_REQUIRE_TITLE`):
  it names the memory in a result list, and recall weighs a match there higher
  than one in the body. Atomic notes are what make dedup, linking, and targeted
  edits possible — you can't cleanly update a fact buried in a 60 KB page.
- **Attribute everything, and make the attribution an ADDRESS.** Set `source` on
  every write — and set it to something a different person can follow back to the
  original a year from now, not a label saying roughly where it came from. Name the
  host and absolute path; the mailbox, sender → recipient, date and subject; the
  messenger, who with whom, and when; for an agent run, the machine, the project and
  the session id or transcript path; for a page, the full URL and the date you read
  it. "email", "from the correspondence", "from the meeting", "the user said" are not
  sources: nothing can be reached through them, and a fact that cannot be re-checked
  can only be believed. `MEMGRES_MAX_SOURCE_BYTES` defaults to 2048 — a real locator
  fits many times over, so there is nothing to economise on. This rule and §2.1 are
  one rule in two halves: the original is not stored, therefore the pointer to it has
  to be good.
- **Link, don't restate.** Write `[[path]]` in the body (or
  `[[path#anchor|label]]`) instead of repeating what another memory says. The links
  become a real graph: `memory_links` walks it in both directions, and the INBOUND
  half is the one that matters — before changing a fact, it tells you who is relying
  on it. A link to something not written yet is fine and deliberate: it stands as
  `resolved: false` and binds itself when the target appears. Moving a memory does
  not break the links to it: the edges follow, and the bodies that name its old
  address are rewritten to the new one (recorded as a `relink`, credited to
  whoever moved it) — so what you read in a body is an address that still works,
  and copying it out of one memory into another stays safe.
- **Notice what is never used.** Each memory records how often it has surfaced in
  a search (`recalled`) and how often it was opened (`gets`) — both on
  `memory_list` rows, and as `usage` on `memory_get`. Surfaced often but never
  opened means it is winning result slots it does not deserve: sharpen its title,
  or fold it into whatever people actually open. Neither surfaced nor opened means
  nobody can reach it, which is a linking and titling problem, not a storage one.
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
- **Erasing a target still strands the text.** A move repairs the bodies that
  name the old address; `forget` cannot — there is no new address to point at, so
  the link is left dangling and visible. Nothing yet offers to repair or remove
  those.
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
