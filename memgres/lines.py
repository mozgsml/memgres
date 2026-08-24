"""Parsing a line selector — ``"2"``, ``"1,3-5"``, ``"40-80"``.

Two features take one: blame, which attributes particular lines, and a
line-ranged read, which returns them. One definition so the two cannot come to
disagree about what ``"3-1"`` or an out-of-range number means.
"""

from __future__ import annotations

from typing import List, Optional


def parse_line_spec(spec: Optional[str],
                    total: Optional[int] = None) -> Optional[List[int]]:
    """1-based line numbers from ``spec``, sorted and deduplicated.

    ``None``/empty means "all lines" and returns ``None``. A reversed range
    (``"5-2"``) reads the same as ``"2-5"`` rather than yielding nothing, since
    nothing else is plausibly meant. With ``total`` given, numbers outside the
    body are dropped instead of erroring: asking for lines 40-80 of a 12-line
    memory is a reasonable thing to do when you don't know the length yet.
    """
    if not spec:
        return None
    out: set = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            out.update(range(lo, hi + 1))
        else:
            out.add(int(part))
    picked = sorted(n for n in out if n >= 1
                    and (total is None or n <= total))
    return picked or None
