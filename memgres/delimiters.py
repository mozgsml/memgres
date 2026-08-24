"""Spotting a client's tool-call delimiter that leaked into a memory's body.

An LLM client emits its tool call in some tag-like format of its own harness;
the harness parses that into JSON and only the JSON travels over MCP. If the
model happens to emit a *closing* tag inside a value, the tag is already inside
the string by the time it reaches us, and it lands in the memory as ordinary
text. On 2026-08-24 five records were corrupted this way, each with a stray
``</body>`` or ``</replace_new>`` at the end.

The failure was silent — the write succeeded and returned a fresh content_hash,
and it was caught only by a person reading the result. That makes it the same
class as a diff that quietly no-ops or a replacement that quietly deletes: a
success in the response, damage in the data.

memgres cannot remove the cause, which is three layers above it. It can see the
symptom, and since it exists to serve LLM clients, this is a typical failure of
its own client population rather than an oddity.

**Warn only — never clean, never refuse.** Bodies legitimately contain markup: a
note about HTML, a code sample with ``</div>``, an XML config. Sanitizing loses
data and rejecting throws away a correct write, and there is never enough
certainty to justify either. So the body is stored exactly as sent, and the
caller is told.
"""

from __future__ import annotations

import re
from typing import List

# The tool's own parameter names — a stray tag is a leak of one of THESE, which
# is what separates it from ordinary markup in prose.
PARAM_NAMES = (
    "body", "replace_new", "replace_old", "path", "tags", "title",
    "source", "reason", "parameter",
)

# How close to the end a stray tag has to be to count. A real leak is what the
# value was truncated with, so it sits at the very end; a mention sits in prose.
TAIL_CHARS = 30

_CLOSING = re.compile(r"</\s*(?:antml:)?([A-Za-z_][\w.-]*)\s*>")


def stray_delimiters(body: str) -> List[str]:
    """Closing tags in ``body`` that look like a leaked delimiter, not markup.

    Three signs must hold together, and the third is the one that makes it work:

    1. the tag is a *closing* tag with no opening partner anywhere in the body;
    2. its name is one of this tool's own parameters;
    3. it sits in the last :data:`TAIL_CHARS` characters.

    Signs 1 and 2 alone are not enough — measured, not assumed. Run over 88 live
    records, "contains a closing tag" gave two false positives, and adding "no
    partner" plus "our parameter name" gave *the same two*: they were records
    **discussing this very failure**, in which ``</body>`` genuinely has no
    partner and genuinely is our parameter name. Adding the position test gave
    zero false positives and zero misses. That category is not a one-off — any
    document about markup (an HTML knowledge base, a note on XML config, someone
    else's bug report quoted verbatim) falls into it.
    """
    if not body:
        return []
    tail = body[-TAIL_CHARS:]
    found: List[str] = []
    for m in _CLOSING.finditer(body):
        name = m.group(1)
        if name not in PARAM_NAMES:
            continue
        if m.start() < len(body) - len(tail):
            continue                              # a mention, not the end
        if re.search(rf"<\s*(?:antml:)?{re.escape(name)}(\s|>)", body):
            continue                              # it has an opening partner
        found.append(m.group(0))
    return found


def write_warnings(body: str) -> List[str]:
    """Warnings to hand back with a successful write. The data is already
    stored; this is what tells the caller to look at it."""
    stray = stray_delimiters(body)
    if not stray:
        return []
    joined = ", ".join(sorted(set(stray)))
    return [f"the body ends with {joined}, which looks like your client's "
            f"tool-call delimiter rather than text you meant to store — the "
            f"write went through unchanged, so check it and rewrite if needed"]
