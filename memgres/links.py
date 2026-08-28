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
from typing import Dict, List, Optional

# Stores other than this one that a memory may legitimately point at. Recorded as
# edges so they are visible, never resolved — we do not own the address space.
KNOWN_SCHEMES = ("idea", "file")

# What a path is lives in `paths.py`, so the parser and the store cannot drift
# apart again — that drift is exactly what dropped every hyphenated link before
# 0.12.0. Kept under the old name because the rest of this module reads better
# with it.
from .paths import PATH_RE as _PATH

_LINK = re.compile(r"\[\[([^\[\]\n]+)\]\]")
_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_CODE_SPAN = re.compile(r"`[^`\n]*`")


@dataclass
class Link:
    """One parsed link. `target` is verbatim as written; `scheme` is None for an
    ordinary path and the scheme name for a pointer at another store.

    `start`/`end` are the link's half-open span in the ORIGINAL body — the whole
    `[[…]]`, brackets included. They are what lets a rewriter replace one link
    without touching the rest of the text, and in particular without touching a
    `[[path]]` written inside backticks: the parser never saw it, so it has no
    span, so a rewrite cannot reach it.
    """
    raw_target: str
    label: Optional[str] = None
    anchor: Optional[str] = None
    scheme: Optional[str] = None
    ord: int = 0
    start: int = 0
    end: int = 0


def _blank_indented(original: str, text: str) -> str:
    """Blank markdown's OTHER code block: a run of lines indented by four spaces
    (or a tab), opened by a blank line.

    Fences are not the only way to show code, and the indented form is what a
    hand-written example tends to use. A frp config pasted that way put
    ``[[proxies]]`` — a TOML array-of-tables header, and a perfectly well-formed
    path — into the link graph as a dangling edge to a memory nobody will ever
    write. The parser must ignore code wherever markdown says code is.

    A blank line inside the block does not end it; the first non-blank line back
    at the margin does. Opening on a blank line is what keeps an ordinary list
    item or a wrapped line — indented, but not preceded by a blank — from being
    read as code.

    🔴 Which lines are code is decided from `original`, and the blanking applied
    to `text` — the copy where spans and fences are already spaces. Deciding from
    the blanked copy instead made every line that STARTED with an inline code
    span look indented: "`[[a]]` but [[b]] counts" became four-plus leading
    spaces, the whole line was swallowed as a block, and a real link next to a
    code span disappeared. Both strings are length-preserving, so their lines
    correspond one to one.
    """
    def blank(line: str) -> str:
        return "".join(" " if c != "\n" else "\n" for c in line)

    src = original.splitlines(keepends=True)
    dst = text.splitlines(keepends=True)
    out, in_block, prev_blank = [], False, True
    for i, line in enumerate(src):
        stripped = line.strip("\n")
        is_blank = not stripped.strip()
        indented = stripped.startswith("    ") or stripped.startswith("\t")
        current = dst[i] if i < len(dst) else line
        if in_block:
            if is_blank or indented:
                out.append(current if is_blank else blank(current))
            else:
                in_block = False
                out.append(current)
        elif indented and prev_blank:
            in_block = True
            out.append(blank(current))
        else:
            out.append(current)
        prev_blank = is_blank
    return "".join(out)


def _blank_code(body: str) -> str:
    """Replace code spans and code blocks — fenced AND indented — with spaces of
    the same length.

    Same length, not removal, so every offset in the returned text still lines up
    with the original — the parser does not need that today, but an anchor
    resolver or a renderer will, and a scanner that quietly shifts positions is a
    bug waiting for the feature that depends on them.
    """
    def blank(m: re.Match) -> str:
        return "".join(" " if c != "\n" else "\n" for c in m.group(0))
    return _blank_indented(body, _CODE_SPAN.sub(blank, _FENCE.sub(blank, body)))


def _classify(target: str) -> Optional[str]:
    """None for a path in this namespace, the scheme name for a known foreign
    store, and the string ``"ignore"`` for everything else."""
    if ":" in target:
        scheme = target.split(":", 1)[0].strip().lower()
        return scheme if scheme in KNOWN_SCHEMES else "ignore"
    return None if _PATH.match(target) else "ignore"


def parse_links(body: Optional[str]) -> List[Link]:
    """Every link in `body`, in the order it appears.

    Matched against the ORIGINAL text, with the blanked copy used only to decide
    whether a match sits inside code. Reading the fields out of the blanked copy
    instead was harmless while nothing wrote them back — and destructive the
    moment something did: a label containing an inline `code` span came back as
    that many spaces, and a rewrite then committed the loss as a well-formed diff.
    A match may also STRADDLE a blanked region (backticks that swallow a `]]` and
    the `[[` after it), which in the blanked copy looks like one enormous link;
    requiring the span to be untouched by blanking rejects that as well.
    """
    body = body or ""
    blanked = _blank_code(body)
    out: List[Link] = []
    for m in _LINK.finditer(body):
        # Is THIS `[[` inside code? Its own opening brackets are blanked if so.
        # Deliberately not "is any part of the span blanked": a label may legally
        # contain an inline `code` span, and rejecting the whole link for that
        # would silently drop a real edge.
        if blanked[m.start():m.start() + 2] != "[[":
            continue
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
                        ord=len(out), start=m.start(), end=m.end()))
    return out


def render(target: str, anchor: Optional[str] = None,
           label: Optional[str] = None) -> str:
    """The canonical text for a link. Inverse of the parse, in its normal form:
    whitespace the author put inside the brackets is not preserved, because there
    is nothing to preserve it for and a single spelling is easier to read."""
    inner = target
    if anchor:
        inner += "#" + anchor
    if label:
        inner += "|" + label
    return f"[[{inner}]]"


def rewrite_targets(body: str, new_targets: Dict[int, str]) -> str:
    """Point selected links somewhere else, leaving the rest of the body alone.

    `new_targets` maps a link's `ord` — its index among the links this parser
    RECOGNISES, which is exactly what the edge table stores — to the target it
    should now carry. Anchor and label ride along unchanged: a rename moves the
    address, not what the author called it or which section they meant.

    Addressed by ord rather than by text on purpose. A body that links to the same
    path twice has two edges, and only one of them may be the one to change; and a
    body that mentions the old path in prose or in a code span must not be touched
    at all. Substituting text would get both of those wrong.

    Assembled in one forward pass over the spans rather than by slicing the whole
    body once per link: a memory at the default 256 KB ceiling can hold tens of
    thousands of links, and rebuilding the string each time made a single rewrite
    quadratic — seconds of CPU while holding row locks.
    """
    if not new_targets:
        return body
    links = sorted((l for l in parse_links(body) if l.ord in new_targets),
                   key=lambda l: l.start)
    if not links:
        return body
    parts, at = [], 0
    for l in links:
        parts.append(body[at:l.start])
        parts.append(render(new_targets[l.ord], l.anchor, l.label))
        at = l.end
    parts.append(body[at:])
    return "".join(parts)
