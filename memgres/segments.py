"""Split a body into overlapping segments for semantic snippets.

A pure function, no DB and no embeddings: it returns *offset spans* (start, end)
into the live body, and the caller slices ``text[start:end]`` later. Offsets are
Python ``str`` indices — code points, not bytes — so they slice unicode correctly.

The cascade:

1. Primary units are sentence spans. We try ``pysbd`` (accurate, unicode-aware);
   if it isn't importable or raises, we fall back to a regex sentence splitter.
2. Whatever the source, we rebuild a *contiguous* partition of ``[0, len(text)]``
   from the span boundaries, so coverage never has gaps regardless of what the
   splitter returned.
3. Consecutive units are greedily merged into non-overlapping tiles no longer
   than ``seg_chars``; a single unit longer than ``seg_chars`` (an unstructured
   wall) is chopped by a fixed char window.
4. Each tile after the first has its start backed up by ``overlap`` chars, so
   neighbours share context. Union of the result always covers ``[0, len(text)]``.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# Fallback sentence splitter: a run of non-terminator chars, its trailing
# terminators, and trailing whitespace. Covers text with no punctuation as one
# span. Used only when pysbd is unavailable or errors.
_SENT_RE = re.compile(r"[^.!?\n]+[.!?]*\s*", re.DOTALL)


def _sentence_spans(text: str) -> List[Tuple[int, int]]:
    """Primary units as (start, end) code-point spans. Prefers pysbd; any
    failure (import or segmentation) falls back to the regex splitter."""
    try:
        import pysbd

        seg = pysbd.Segmenter(language="en", clean=False, char_span=True)
        spans = seg.segment(text)
        out = [(int(s.start), int(s.end)) for s in spans]
        if out:
            return out
    except Exception:
        pass
    return [(m.start(), m.end()) for m in _SENT_RE.finditer(text)]


def _units(text: str) -> List[Tuple[int, int]]:
    """A contiguous partition of [0, len(text)] derived from the sentence-span
    boundaries. Rebuilding from boundary *points* guarantees no gaps even if the
    splitter left whitespace uncovered or produced overlapping spans."""
    n = len(text)
    cuts = {0, n}
    for s, e in _sentence_spans(text):
        if 0 <= s <= n:
            cuts.add(s)
        if 0 <= e <= n:
            cuts.add(e)
    ordered = sorted(cuts)
    return [(ordered[i], ordered[i + 1]) for i in range(len(ordered) - 1)]


def segment(text: str, seg_chars: int = 400, overlap: int = 80) -> List[Tuple[int, int]]:
    """Return (start, end) code-point offsets that fully cover ``text``, each
    roughly ``<= seg_chars``, with ``~overlap`` chars shared between consecutive
    segments. ``text[start:end]`` slices correctly (offsets are str indices).

    Empty text → ``[]``; text shorter than ``seg_chars`` → a single
    ``(0, len(text))``. The union of the returned ranges always covers
    ``[0, len(text)]`` (overlaps are fine, gaps are not)."""
    n = len(text)
    if n == 0:
        return []
    overlap = max(0, min(overlap, seg_chars - 1))  # keep overlap < seg_chars
    if n <= seg_chars:
        return [(0, n)]

    # 1+2: contiguous primary units.
    units = _units(text)

    # 3: greedily merge units into non-overlapping tiles <= seg_chars; chop any
    #    single oversized unit by a fixed char window.
    tiles: List[Tuple[int, int]] = []
    cur_s = cur_e = 0
    for s, e in units:
        if e - s > seg_chars:
            if cur_e > cur_s:
                tiles.append((cur_s, cur_e))
                cur_s = cur_e = e
            p = s
            while p < e:
                q = min(p + seg_chars, e)
                tiles.append((p, q))
                p = q
            cur_s = cur_e = e
            continue
        if cur_e == cur_s:            # nothing buffered yet
            cur_s, cur_e = s, e
        elif e - cur_s <= seg_chars:  # units are contiguous, so cur_e == s
            cur_e = e
        else:
            tiles.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    if cur_e > cur_s:
        tiles.append((cur_s, cur_e))

    # 4: back up each later tile's start by ~overlap so neighbours share context.
    out: List[Tuple[int, int]] = []
    for i, (s, e) in enumerate(tiles):
        start = 0 if i == 0 else max(0, s - overlap)
        out.append((start, e))
    return out
