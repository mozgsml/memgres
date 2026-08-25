"""`[[wiki links]]` in a body, parsed into edges.

The convention arrived on its own: this repo's reference corpus already carried
238 `[[…]]` links across 97 memories with no tool support at all, 91% of them
resolving. What it did not carry was a way to ask "what points HERE" — 42 of
those 97 memories had no inbound link and were reachable only by search — or any
way to notice when a link stopped resolving.

## The syntax

    [[target]]                a link
    [[target#anchor]]         …to a section of it
    [[target|label]]          …shown as something else
    [[target#anchor|label]]   both

`#` before `|`, as in every wiki dialect since MediaWiki and in Obsidian today,
so a link written from habit parses. The split is positional and not clever:
first `|` ends the target part, first `#` inside that part starts the anchor. A
`#` in the LABEL is therefore just text, which is the reading that surprises
nobody.

## What is a link, and what is merely square brackets

Only two shapes are treated as links, and everything else is left alone:

* a **path** — an ltree path in this memory's own namespace (`ops.x402.deploy`);
* a **scheme** we know (`idea:some-slug`, `file:some-note`) — a pointer at
  another store entirely, recorded as an edge but never resolved here.

A URL is not a link for these purposes and neither is prose. The rule is stated
POSITIVELY — recognise a path or a known scheme — rather than as a list of things
to exclude, because the exclusions are unbounded and the inclusions are two.

Two exclusions are still needed, and both come straight from the corpus:

* **code spans and fenced blocks are not scanned.** Documentation that explains
  the syntax writes ``[[path]]`` in backticks, and a validator that flags its own
  documentation trains everyone to ignore it;
* single-segment targets that name nothing here are still recorded as edges
  rather than dropped. A link to something not yet written is a deliberate move
  ("this deserves a memory") and the tooling must let it stand — dangling, and
  visible as dangling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# Stores other than this one that a memory may legitimately point at. Recorded as
# edges so they are visible, never resolved — we do not own the address space.
KNOWN_SCHEMES = ("idea", "file")

# An ltree path: labels of [A-Za-z0-9_] joined by dots. Deliberately strict —
# this is what tells a path apart from a slug belonging to another store, and
# from prose that happens to sit in double brackets.
_PATH = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")

_LINK = re.compile(r"\[\[([^\[\]\n]+)\]\]")
_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_CODE_SPAN = re.compile(r"`[^`\n]*`")


@dataclass
class Link:
    """One parsed link. `target` is verbatim as written; `scheme` is None for an
    ordinary path and the scheme name for a pointer at another store."""
    raw_target: str
    label: Optional[str] = None
    anchor: Optional[str] = None
    scheme: Optional[str] = None
    ord: int = 0


def _blank_code(body: str) -> str:
    """Replace code spans and fenced blocks with spaces of the same length.

    Same length, not removal, so every offset in the returned text still lines up
    with the original — the parser does not need that today, but an anchor
    resolver or a renderer will, and a scanner that quietly shifts positions is a
    bug waiting for the feature that depends on them.
    """
    def blank(m: re.Match) -> str:
        return "".join(" " if c != "\n" else "\n" for c in m.group(0))
    return _CODE_SPAN.sub(blank, _FENCE.sub(blank, body))


def _classify(target: str) -> Optional[str]:
    """None for a path in this namespace, the scheme name for a known foreign
    store, and the string ``"ignore"`` for everything else."""
    if ":" in target:
        scheme = target.split(":", 1)[0].strip().lower()
        return scheme if scheme in KNOWN_SCHEMES else "ignore"
    return None if _PATH.match(target) else "ignore"


def parse_links(body: Optional[str]) -> List[Link]:
    """Every link in `body`, in the order it appears."""
    out: List[Link] = []
    for m in _LINK.finditer(_blank_code(body or "")):
        inner = m.group(1).strip()
        target_part, _, label = inner.partition("|")
        target, _, anchor = target_part.partition("#")
        target, label, anchor = (target.strip(), label.strip(), anchor.strip())
        if not target:
            continue
        kind = _classify(target)
        if kind == "ignore":
            continue
        out.append(Link(raw_target=target, label=label or None,
                        anchor=anchor or None,
                        scheme=None if kind is None else kind,
                        ord=len(out)))
    return out
