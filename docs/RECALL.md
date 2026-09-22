# Recall: how it ranks, and how to tune it to your corpus

memgres searches two ways at once and merges the results. The merge has settings,
and the right values depend on what *you* store — so there is a tool that
measures them against your own memories instead of asking you to guess.

## The two searches

**Lexical** (Postgres full-text) matches words. It keeps exact strings whole:
Postgres parses `192.168.1.121` and `agentstools.dev` as single tokens, so a
query for either finds precisely the memory that contains it. It knows nothing
about meaning — ask it about "payments failing" and it answers with everything
containing "payments".

**Semantic** (a vector backend) matches meaning. It finds a memory that says
"the settlement was rejected" when you ask about "payments failing", and it
works across languages. It is weakest exactly where the lexical search is
strongest: an IP, a uuid, a config key or an identifier carries no meaning to
compress, so every similar-looking string sits about as close, and the one
memory you wanted lands mid-list.

`mode="auto"` — the default for every caller — runs **hybrid** wherever an
embedder is configured, and falls back to lexical when there is none. Ask for
`semantic` explicitly when you want wording ignored on purpose.

## How the two get merged (RRF)

Their scores are not comparable: `ts_rank` measures match density on an open
scale, a vector match is a cosine in [0, 1], and normalising them per query is
guesswork. So memgres fuses **ranks**, not scores — Reciprocal Rank Fusion. Each
ranking votes for a memory by its position, and the votes add up:

```
score(memory) = Σ  weight_of_that_ranking / (MEMGRES_RRF_K + position)
```

A memory both searches rank well beats one that tops a single list. That is the
whole idea: agreement between two independent methods is a stronger signal than
confidence from either.

What RRF cannot see is confidence. A lexical hit that matched one common word
sits at position 1 and votes exactly as loudly as a perfect match would. That is
what the weights are for.

| setting | default | what it does |
|---|---|---|
| `MEMGRES_RRF_K` | `60` | Damping. Larger flattens the top, so being ranked well by *both* searches outweighs topping one. Smaller makes a first place dominate. |
| `MEMGRES_RRF_W_SEMANTIC` | `1.0` | How loudly the vector ranking votes. |
| `MEMGRES_RRF_W_LEXICAL` | `1.0` | How loudly the lexical ranking votes on an ordinary prose query. |
| `MEMGRES_RRF_W_LEXICAL_LITERAL` | `2.0` | Its weight when the query carries a literal — an IP, a uuid, a `/path`, `AN_ENV_KEY`, an `identifier`, a `host.name`. Detected from the query text alone. |

Raising `W_LEXICAL_LITERAL` says: *on an exact string, trust the exact match.*
Lowering `W_LEXICAL` says the mirror thing about prose — trust meaning, and let
word matches only break ties. The first is the shipped default because it
measured well; the second sounds just as reasonable and measured **worse** (see
the worked example). Change neither without a run behind it.

## Measuring, so the numbers are not a guess

```bash
memgres-eval                    # the modes as they stand, on generated cases
memgres-eval --sweep            # … and a grid of fusion settings
memgres-eval --failures         # … listing what beat the right answer
memgres-eval --queries mine.jsonl --json report.json
```

It reads the deployment's own environment, so it measures the configuration you
actually run.

**Where the cases come from.** Labelling queries by hand is the reason most
deployments never measure retrieval at all, so two kinds are derived from the
corpus itself:

- `title` — a memory's curated title is the query and that memory is the answer.
  A title describes the memory in other words than its body, so this asks: can
  the search find a memory from a description of it?
- `literal` — a string occurring in exactly one memory is the query and that
  memory is the answer. Unambiguous by construction, and the case a vector alone
  handles worst.

**Both are biased toward the lexical search, and you must read the table
knowing it.** A title is stored in the lexical index (weighted, at that) and a
literal is taken verbatim out of a body, so any query generated this way is made
of words that are literally present. That is why a `title MRR` of 1.000 for the
lexical mode means very little. Use these cases for two honest purposes —
catching a regression, and tuning the *literal* side — and get the unbiased set
from `MEMGRES_SEARCH_LOG`, which records what callers actually asked, or from
`--queries FILE` — JSON Lines, one `{"query": "...", "expect": "<path or id>"}`
per line.

**Reading the table.** `MRR` is the headline: 1.0 means the right memory was
always first, 0.5 means typically second. The `hit@1 / hit@3 / hit@10` columns
matter more than MRR for agents, because an agent reads the top few results and
rarely reaches the tenth. The per-kind columns are the diagnosis: a `literal` MRR far below the others
means exact strings are getting lost — raise `W_LEXICAL_LITERAL`, or check that
there is a lexical index at all.

**Why the sweep is cheap.** Both rankings are fetched once per case, deep, and
every setting is then scored against those stored lists in memory. Twenty
settings cost one query each, not twenty, nothing is re-embedded, and every row
of the table is measured on identical inputs.

## The search log: cases from real traffic

`MEMGRES_SEARCH_LOG=true` records two things: every recall (the query, the mode
it resolved to, the ranked ids it returned, how long it took) and every get (the
memory that was opened). A get shortly after a recall by the same caller is the
answer to that query, and `memgres-eval --from-log N` turns those pairs into
cases.

```bash
memgres-eval --from-log 200 --log-source mcp --sweep
```

`--log-source` keeps the doors apart — `mcp` is agents, `web` is people in the
panel, `rest` is scripts — because they ask differently and a mixed measurement
describes neither.

Three things to be clear-eyed about:

- **A query is content.** It often reveals more than the memory it found, which
  is why this is off by default, why rows carry no bodies, and why they are
  swept after `MEMGRES_SEARCH_LOG_DAYS` (30 by default, on their own clock —
  independent of how long memories are kept).
- **It only sees searches that worked.** A query nobody could answer never
  becomes a case, so a log-derived score flatters a ranking that fails quietly.
  Keep the generated cases alongside.
- **It inherits position bias.** People and agents open what was shown first, so
  the log partly measures the ranking that produced it. That is fine for
  comparing settings and wrong for training a ranker on — the distinction that
  the *unbiased learning-to-rank* literature exists to handle.

## Tuning in practice

0. If you can, turn on `MEMGRES_SEARCH_LOG` first and let it run for a while —
   real queries are the only unbiased cases (see below).
1. Run `memgres-eval --sweep` on a deployment with real memories (a few hundred
   are plenty).
2. Pick the row that wins on `hit@3` for the kind of query you care about,
   preferring a setting that does not lose ground on the other kind.
3. Put those values in the environment of every process that *reads* — the MCP
   server, the REST/panel service — and re-run to confirm.
4. Re-measure after the corpus changes shape (a large import, a new language).
   Nothing about the tuning is permanent; that is why it is configuration.

A caution: a grid measured on 50 cases can be fit to noise. Prefer a setting
that is good over a broad neighbourhood of values to one that is best at a
single point.

## A worked example (the shipped defaults came from this)

A 160-case run on a real corpus — 80 literal, 80 title — over 198 memories,
averaging ~4 KB of body each, mixed Russian and English:

| setting | MRR | hit@1 | hit@3 | hit@10 | literal MRR |
|---|---|---|---|---|---|
| lexical | 0.849 | 0.844 | 0.850 | 0.863 | 0.697 |
| semantic | 0.615 | 0.556 | 0.637 | 0.750 | 0.381 |
| hybrid, weights 1/1 | 0.834 | 0.787 | 0.869 | 0.912 | 0.759 |
| hybrid, literal weight 2 | 0.860 | 0.838 | 0.869 | 0.912 | **0.809** |

Three things are worth taking from it.

**Hybrid trades a little top-1 for much less total failure.** Lexical alone has
the better MRR yet finds nothing at all for one query in seven (`hit@10` 0.863);
hybrid answers nine in ten.

**The literal weight does what the mechanism predicts** — it lifts the literal
cases under every other setting and costs nothing on the rest. That is why it
ships at 2.0.

**The obvious-looking idea was wrong.** Damping the lexical vote on prose
queries (`W_LEXICAL` 0.3–0.5), to stop a one-common-word match from voting as
loudly as a perfect one, made every column worse. The junk it removes is
apparently outweighed by the true hits it demotes — on this corpus. Which is the
argument for the tool: intuitions about ranking are cheap and usually wrong, and
your corpus may not behave like this one.
