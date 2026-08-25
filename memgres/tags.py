"""Tag normalisation — one spelling of a tag, so a filter can actually find it.

Tags are matched byte-for-byte by a GIN index, which means every difference in
case or Unicode form makes a second, silently unrelated tag. This repo's own
corpus reached 265 distinct tags across 97 memories that way: almost every tag
written once, filtering by them useless not because the index was broken but
because nothing agreed on the spelling.

Two normalisations, and the second is the one that is easy to miss:

* **case** — `X402` and `x402` are the same label to a person;
* **Unicode form (NFC)** — "й" is either U+0439 or "и" + a combining breve, and
  Cyrillic text arrives both ways depending on the editor. They look identical
  and compare unequal, which is the worst kind of difference to debug.

``lower()`` rather than ``casefold()`` on purpose: the one-off migration that
normalises existing rows runs in SQL (``lower(normalize(...))``), and the two
have to produce the same string or old tags stop matching new ones. casefold is
the better caseless-comparison primitive in the abstract; agreeing with the data
already in the table matters more.
"""

from __future__ import annotations

import unicodedata
from typing import Iterable, List, Optional, Sequence

# How a set of requested tags combines. "all" = every tag must be present
# (Postgres `@>`), "any" = at least one (`&&`). Both ride the same GIN index.
TAG_MATCH = ("all", "any")


def normalize_tag(tag: str) -> str:
    return unicodedata.normalize("NFC", tag).strip().lower()


def normalize_tags(tags: Optional[Iterable[str]]) -> Optional[List[str]]:
    """Normalise, drop empties, de-duplicate — preserving first-seen order so a
    caller's tags come back in the order they wrote them.

    Returns None for None (meaning "leave tags alone" on a write), and an empty
    list for an empty input (meaning "clear them"). The distinction is load-
    bearing: `_update` uses `tags is None` to decide whether to touch them.
    """
    if tags is None:
        return None
    out: List[str] = []
    for t in tags:
        n = normalize_tag(t)
        if n and n not in out:
            out.append(n)
    return out


def check_tag_match(value: Optional[str]) -> str:
    if value is None:
        return "all"
    if value not in TAG_MATCH:
        raise ValueError(
            f"unknown tag match: {value!r} — use 'all' (every tag) or 'any'")
    return value


def tag_counts(conn, ns: Sequence[str], *, prefix: Optional[str] = None,
               k: int = 50) -> List[dict]:
    """The tag vocabulary actually in use, most-used first.

    Exists because a caller cannot reuse a tag it has never seen: without this,
    each agent invents its own spelling and the tag filter degrades into a set of
    single-use labels. Bounded by `k` and narrowable by `prefix`, so the answer
    stays small on a corpus with thousands of tags.
    """
    from .vector.base import as_namespaces
    sql = ["SELECT t, count(*) AS n FROM memory, unnest(tags) AS t",
           "WHERE namespace = ANY(%s)",
           "  AND (expires_at IS NULL OR expires_at > now())"]
    params: list = [as_namespaces(ns)]
    if prefix:
        sql.append("  AND t LIKE %s")
        params.append(normalize_tag(prefix) + "%")
    sql.append("GROUP BY t ORDER BY n DESC, t ASC LIMIT %s")
    params.append(k)
    with conn.cursor() as cur:
        cur.execute("\n".join(sql), params)
        return [{"tag": r[0], "count": int(r[1])} for r in cur.fetchall()]
