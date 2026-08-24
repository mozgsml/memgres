"""Parsing a line selector — ``"2"``, ``"1,3-5"``, ``"40-80"``.

Two features take one: blame, which attributes particular lines, and a
line-ranged read, which returns them. One definition so the two cannot come to
disagree about what ``"3-1"`` or an out-of-range number means.
"""

from __future__ import annotations

from typing import List, Optional


# The most lines one selector may name. Without a ceiling, `"1-50000000"` builds
# fifty million integers before anything looks at how long the body actually is:
# measured at 4.3 GB and 3.4 seconds, from one request, to return ten numbers.
MAX_SPAN = 100_000


def parse_line_spec(spec: Optional[str],
                    total: Optional[int] = None) -> Optional[List[int]]:
    """1-based line numbers from ``spec``, sorted and deduplicated.

    ``None``/empty means "all lines" and returns ``None``. A reversed range
    (``"5-2"``) reads the same as ``"2-5"`` rather than yielding nothing, since
    nothing else is plausibly meant. With ``total`` given, a range is clipped to
    the body **before** it is expanded — so asking for lines 40-80 of a 12-line
    memory is answered with what exists, cheaply, rather than by building 80
    numbers and discarding most.

    A selection that is still absurdly large after clipping is refused rather
    than trimmed: the caller asked for something impossible, and silently
    handing back the first hundred thousand lines would look like an answer.
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
            lo = max(lo, 1)
            if total is not None:
                hi = min(hi, total)
            if hi < lo:
                continue
            if hi - lo + 1 > MAX_SPAN or len(out) + (hi - lo + 1) > MAX_SPAN:
                raise ValueError(
                    f"a line selector may name at most {MAX_SPAN} lines; "
                    f"'{part}' asks for {hi - lo + 1}")
            out.update(range(lo, hi + 1))
        else:
            out.add(int(part))
            if len(out) > MAX_SPAN:      # "1,2,3,…" a million times over
                raise ValueError(
                    f"a line selector may name at most {MAX_SPAN} lines")
    if len(out) > MAX_SPAN:
        raise ValueError(f"a line selector may name at most {MAX_SPAN} lines")
    picked = sorted(n for n in out if n >= 1
                    and (total is None or n <= total))
    return picked or None
