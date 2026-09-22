"""Measure recall quality against YOUR memories, and tune the fusion to them.

memgres ships defaults for hybrid fusion (``MEMGRES_RRF_K`` and the three
``MEMGRES_RRF_W_*`` weights), but the right numbers depend on what a deployment
actually stores: a corpus of prose and a corpus of hostnames and config keys
reward different settings. This tool answers that empirically instead of by
taste — run it on a real deployment, read the table, set the env.

**Where the ground truth comes from.** Hand-labelling queries is the reason
nobody measures their retrieval, so two kinds are derived from the corpus itself:

* ``title`` — a memory's curated title is the query, that memory is the answer.
  A title is a human sentence about the memory written in other words than its
  body, so this asks: can the search find a memory from a description of it?
* ``literal`` — a string that occurs in exactly ONE memory (an IP, a host, a
  path, ``AN_ENV_KEY``, an ``identifier``) is the query, that memory is the
  answer. There is only one correct result by construction, and a vector alone
  is weakest here, so this is where fusion earns its keep.

Neither is a substitute for what people really ask — and both are made of words
taken out of the corpus, which flatters whichever search matches words. For the
unbiased set, turn on ``MEMGRES_SEARCH_LOG`` and pass ``--from-log N``: the
query is one that was really asked, and the answer is the memory the caller
opened right after. ``--queries FILE`` (JSON Lines:
``{"query": "...", "expect": "<path or id>"}``) takes a hand-written set. The
generated kinds remain as the always-available baseline that catches
regressions.

**Why the sweep is cheap.** For each case the two rankings — lexical and vector
— are fetched ONCE, deep. Every fusion setting is then applied to those stored
lists in memory, so trying twenty settings costs one query, not twenty, and no
embedding is recomputed. This also means a sweep measures the fusion and nothing
else: the inputs are identical for every row.

Usage::

    memgres-eval                          # generated cases, current settings
    memgres-eval --sweep                  # … plus a grid over rrf_k and weights
    memgres-eval --from-log 200           # … real queries from the search log
    memgres-eval --queries mine.jsonl     # … and/or your own labelled queries
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from .search import fuse, lexical_search, looks_literal

# A case is (kind, query, expected memory id).
Case = Tuple[str, str, str]
# One fusion setting under test.
Setting = Tuple[str, dict]

FETCH = 50          # how deep each ranking is pulled before fusion
REPORT_AT = (1, 3, 10)


# ─── building cases out of the corpus ────────────────────────────────────────
def _title_cases(conn, ns: Sequence[str], n: int, rng: random.Random) -> List[Case]:
    """A memory's title as the query. Titles shorter than a few words are
    skipped: 'Deploy' names a memory for a human who already knows the corpus,
    but as a query it has no single right answer."""
    with conn.cursor() as cur:
        cur.execute(
            # ORDER BY id, then shuffle with a seeded rng: without the ORDER BY
            # the sample rides on PHYSICAL row order, which Postgres rewrites
            # whenever the rows are updated — so a re-embed silently changed the
            # case set and two runs were no longer comparable. Found by noticing
            # that the lexical column, which touches no vectors at all, moved
            # between two passes of an A/B.
            "SELECT id, title FROM memory WHERE namespace = ANY(%s::text[]) "
            "AND title IS NOT NULL AND length(title) > 20 AND length(body) > 200 "
            "ORDER BY id",
            (list(ns),))
        rows = cur.fetchall()
    rng.shuffle(rows)
    return [("title", str(t).strip(), str(i)) for i, t in rows[:n]]


_SKIP = re.compile(r"^\d+\.\d+\.\d+$")      # a bare version number names nothing


def _literal_cases(conn, ns: Sequence[str], n: int, rng: random.Random,
                   per_memory: int = 2) -> List[Case]:
    """Strings that occur in exactly one memory, as queries.

    Uniqueness is what makes the answer unambiguous, so the token → memories map
    is built over the whole corpus and anything seen twice is dropped. At most
    ``per_memory`` survive from any one memory, or a single long changelog would
    supply half the cases and the measurement would describe that memory rather
    than the corpus."""
    from .search import LITERAL_RE

    with conn.cursor() as cur:
        cur.execute("SELECT id, body FROM memory WHERE namespace = ANY(%s::text[]) "
                    "AND body <> '' ORDER BY id", (list(ns),))   # see _title_cases
        rows = cur.fetchall()
    where: Dict[str, set] = {}
    for mid, body in rows:
        for m in LITERAL_RE.finditer(body or ""):
            tok = m.group(0).strip(".,;:()[]{}\"'")
            if len(tok) < 7 or _SKIP.match(tok):
                continue
            where.setdefault(tok.lower(), set()).add((str(mid), tok))
    unique = [next(iter(v)) for v in where.values() if len({i for i, _ in v}) == 1]
    rng.shuffle(unique)
    out: List[Case] = []
    seen: Dict[str, int] = {}
    for mid, tok in unique:
        if seen.get(mid, 0) >= per_memory:
            continue
        seen[mid] = seen.get(mid, 0) + 1
        out.append(("literal", tok, mid))
        if len(out) >= n:
            break
    return out


def _log_cases(conn, ns: Sequence[str], n: int, window_s: int = 180,
               source: Optional[str] = None) -> List[Case]:
    """Real queries, answered by what the caller opened next.

    This is the only unbiased ground truth available: a generated case is made
    of words taken out of the corpus, so it flatters whichever search matches
    words. Here the query is whatever someone actually asked, and the answer is
    the memory they went on to read in full — a `get` by the same caller within
    `window_s` of the recall.

    Two honest weaknesses. It can only learn about searches that SUCCEEDED, so
    a query nobody could answer never becomes a case (raising the score of a
    ranking that fails silently). And what was opened is biased by what the
    ranking showed first — the usual position bias of click data. Good enough to
    tune against, not to train on.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.query, g.memory_id FROM search_log r "
            "JOIN LATERAL ("
            "   SELECT g.memory_id, g.at FROM search_log g "
            # `id`, not `at`, decides what came after what: `now()` is the
            # TRANSACTION's clock, so a recall and the get that followed it
            # inside one transaction carry the identical timestamp. The interval
            # still bounds how far ahead to look.
            "    WHERE g.kind = 'get' AND g.id > r.id "
            "      AND g.at <= r.at + %s::interval "
            "      AND g.user_id IS NOT DISTINCT FROM r.user_id "
            "      AND g.token_id IS NOT DISTINCT FROM r.token_id "
            "    ORDER BY g.id LIMIT 1) g ON true "
            " WHERE r.kind = 'recall' AND r.query <> '' "
            "   AND (%s::text IS NULL OR r.source = %s) "
            "   AND g.memory_id = ANY(r.results) "        # it was IN the results
            "   AND EXISTS (SELECT 1 FROM memory m WHERE m.id = g.memory_id "
            "               AND m.namespace = ANY(%s::text[])) "
            " ORDER BY r.at DESC LIMIT %s",
            (f"{window_s} seconds", source, source, list(ns), n))
        rows = cur.fetchall()
    # the same question asked twice is one case, not two
    seen = {}
    for query, mid in rows:
        seen.setdefault((query.strip(), str(mid)), None)
    return [("logged", q, m) for q, m in seen]


def _file_cases(conn, ns: Sequence[str], path: str) -> List[Case]:
    """Labelled queries from a file — the only kind that knows what people ask.
    ``expect`` is a memory path or id; a path is resolved here so the file stays
    readable and survives a re-import that changes ids."""
    out: List[Case] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            # A file may carry the kind each case came from (--dump-cases writes
            # it): keeping it is what makes an A/B readable, since the per-kind
            # columns are where a change shows its shape. Without it every case
            # lands in one bucket and a treatment that only helps one kind looks
            # like a small general gain.
            kind = str(row.get("kind") or "file")
            want = str(row["expect"])
            if "-" not in want or " " in want:      # not a uuid: treat as a path
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM memory WHERE namespace = ANY(%s::text[]) "
                                "AND path = %s", (list(ns), want))
                    got = cur.fetchone()
                if got is None:
                    print(f"  ! no memory at path {want!r} — skipped", file=sys.stderr)
                    continue
                want = str(got[0])
            out.append((kind, row["query"], want))
    return out


# ─── running them ────────────────────────────────────────────────────────────
def _rankings(conn, cfg, embedder, backend, ns, query: str):
    """The two rankings for one query, fetched once and reused by every setting."""
    lex = lexical_search(conn, cfg, ns, query, FETCH, None, None, None, "all")
    sem = (backend.search(conn, cfg, embedder.embed_query(query), FETCH, ns,
                          None, None, "all") if backend else [])
    return lex, sem


def _rank_of(hits, want: str) -> Optional[int]:
    for i, h in enumerate(hits):
        if str(h.id) == want:
            return i + 1
    return None


def _fuse(lex, sem, cfg, query: str, k: int):
    w_lex = cfg.rrf_w_lexical_literal if looks_literal(query) else cfg.rrf_w_lexical
    return fuse([sem, lex], k, rrf_k=cfg.rrf_k,
                weights=[cfg.rrf_w_semantic, w_lex])


def _score(ranks: Sequence[Optional[int]]) -> dict:
    """MRR plus hit-rates at the depths an agent actually reads.

    MRR alone hides the difference that matters to a caller: a memory ranked 1st
    is read, one ranked 8th is usually not, yet MRR calls that 1.0 vs 0.125 and
    averages it away. The hit-rates say it plainly."""
    n = len(ranks) or 1
    out = {"n": len(ranks),
           "mrr": round(sum(1.0 / r for r in ranks if r) / n, 4)}
    for at in REPORT_AT:
        out[f"hit@{at}"] = round(sum(1 for r in ranks if r and r <= at) / n, 4)
    out["missed"] = sum(1 for r in ranks if not r)
    return out


def evaluate(conn, cfg, embedder, backend, ns, cases: Sequence[Case],
             settings: Sequence[Setting], k: int = 10) -> dict:
    """Run every case once, then score every setting over the stored rankings."""
    fetched = []
    for kind, query, want in cases:
        lex, sem = _rankings(conn, cfg, embedder, backend, ns, query)
        fetched.append((kind, query, want, lex, sem))

    kinds = sorted({c[0] for c in cases})
    report: dict = {"cases": len(cases), "by_kind": {}, "settings": {}}
    for name, over in settings:
        per_kind: Dict[str, List[Optional[int]]] = {kk: [] for kk in kinds}
        worst: List[dict] = []
        c2 = dataclasses.replace(cfg, **over) if over else cfg
        for kind, query, want, lex, sem in fetched:
            if name == "lexical":
                hits = lex[:k]
            elif name == "semantic":
                hits = sem[:k]
            else:
                hits = _fuse(lex, sem, c2, query, k)
            r = _rank_of(hits, want)
            per_kind[kind].append(r)
            if r is None or r > 3:
                worst.append({"kind": kind, "query": query, "rank": r,
                              "ahead": [h.path for h in hits[:3]]})
        report["settings"][name] = {
            "params": over,
            "all": _score([r for rs in per_kind.values() for r in rs]),
            "by_kind": {kk: _score(v) for kk, v in per_kind.items()},
            "failures": worst[:20],
        }
    report["by_kind"] = {kk: sum(1 for c in cases if c[0] == kk) for kk in kinds}
    return report


def default_settings(cfg, sweep: bool) -> List[Setting]:
    """What to compare. Always the three modes as they stand; with ``--sweep``
    also a grid around the current fusion — coarse on purpose, since the point
    is to see the shape of the response, not to overfit a corpus that grows."""
    out: List[Setting] = [("lexical", {}), ("semantic", {}), ("hybrid (current)", {})]
    if not sweep:
        return out
    for rrf_k in (10, 60):
        for w_lex in (0.3, 0.5, 1.0):
            for w_lit in (1.0, 2.0):
                out.append((f"k={rrf_k} lex={w_lex} lit={w_lit}",
                            {"rrf_k": rrf_k, "rrf_w_lexical": w_lex,
                             "rrf_w_lexical_literal": w_lit}))
    return out


# ─── CLI ─────────────────────────────────────────────────────────────────────
def _print(report: dict, k: int, show_failures: bool) -> None:
    kinds = list(report["by_kind"])
    head = f"{'setting':22} {'MRR':>6} " + " ".join(f"{'hit@'+str(a):>7}" for a in REPORT_AT)
    head += "  " + " ".join(f"{kk[:8]+' MRR':>13}" for kk in kinds)
    print(f"\n{report['cases']} cases " +
          ", ".join(f"{n} {kk}" for kk, n in report["by_kind"].items()) +
          f"; depth k={k}\n")
    print(head)
    print("-" * len(head))
    for name, r in report["settings"].items():
        row = f"{name:22} {r['all']['mrr']:>6.3f} " + " ".join(
            f"{r['all']['hit@'+str(a)]:>7.3f}" for a in REPORT_AT)
        row += "  " + " ".join(f"{r['by_kind'][kk]['mrr']:>13.3f}" for kk in kinds)
        print(row)
    if show_failures:
        for name, r in report["settings"].items():
            if not r["failures"]:
                continue
            print(f"\n{name} — where the answer was not in the top 3:")
            for f in r["failures"][:10]:
                print(f"  [{f['kind']}] {f['query'][:70]!r} → rank {f['rank']}")
                for p in f["ahead"]:
                    print(f"        above it: {p}")


def main() -> None:  # pragma: no cover - entrypoint
    import psycopg

    from .config import load
    from .embeddings import get_embedder
    from .vector.base import make_backend

    ap = argparse.ArgumentParser(
        prog="memgres-eval",
        description="Measure recall quality on this deployment's own memories.")
    ap.add_argument("--titles", type=int, default=60, help="generated title cases (0 = none)")
    ap.add_argument("--literals", type=int, default=60, help="generated literal cases (0 = none)")
    ap.add_argument("--queries", help="JSON Lines file of labelled queries")
    ap.add_argument("--from-log", type=int, default=0, metavar="N",
                    help="also take up to N real cases from the search log "
                         "(needs MEMGRES_SEARCH_LOG; see docs/RECALL.md)")
    ap.add_argument("--log-source", help="limit --from-log to one door: mcp | web | rest")
    ap.add_argument("--space-id", action="append", help="limit to these namespace ids")
    ap.add_argument("--k", type=int, default=10, help="depth the metrics judge (default 10)")
    ap.add_argument("--sweep", action="store_true", help="also try a grid of fusion settings")
    ap.add_argument("--failures", action="store_true", help="list the cases that missed")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--dump-cases", metavar="FILE",
                    help="write the cases to a JSON Lines file and exit — feed it "
                         "back with --queries to measure two builds of the index "
                         "on exactly the same questions")
    ap.add_argument("--seed", type=int, default=0, help="case sampling seed (repeatable)")
    args = ap.parse_args()

    cfg = load()
    embedder = get_embedder(cfg)
    backend = make_backend(cfg, embedder)
    if backend is None:
        print("No embedder configured — only lexical recall exists here, and "
              "there is nothing to fuse or tune.", file=sys.stderr)
    conn = psycopg.connect(cfg.database_url or "")
    conn.autocommit = True

    ns = args.space_id
    if not ns:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT namespace FROM memory")
            ns = [str(r[0]) for r in cur.fetchall()]
    if not ns:
        raise SystemExit("no memories here yet — nothing to measure.")

    rng = random.Random(args.seed)
    cases: List[Case] = []
    if args.titles:
        cases += _title_cases(conn, ns, args.titles, rng)
    if args.literals:
        cases += _literal_cases(conn, ns, args.literals, rng)
    if args.from_log:
        logged = _log_cases(conn, ns, args.from_log, source=args.log_source)
        if not logged:
            print("  ! the search log has no answered queries yet "
                  "(MEMGRES_SEARCH_LOG off, or nothing opened after a recall)",
                  file=sys.stderr)
        cases += logged
    if args.queries:
        cases += _file_cases(conn, ns, args.queries)
    if not cases:
        raise SystemExit("no cases: the corpus has no titles or literals to "
                         "derive them from — pass --queries with your own.")

    if args.dump_cases:
        with open(args.dump_cases, "w", encoding="utf-8") as fh:
            for kind, query, want in cases:
                fh.write(json.dumps({"query": query, "expect": want, "kind": kind},
                                    ensure_ascii=False) + "\n")
        print(f"{len(cases)} cases → {args.dump_cases}")
        return

    report = evaluate(conn, cfg, embedder, backend, ns, cases,
                      default_settings(cfg, args.sweep), k=args.k)
    _print(report, args.k, args.failures)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print(f"\nfull report → {args.json}")


if __name__ == "__main__":  # pragma: no cover
    main()
