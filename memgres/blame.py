"""Turn the diff history into an annotated document (git-blame) and reconstruct
any past version — so callers never replay diffs themselves.

Because the store keeps a canonical diff for *every* body change (create is a
diff-from-empty), the history is a self-contained chain empty → current. We
replay it forward line by line, carrying an attribution with each surviving line:

  * an added line is attributed to the change that introduced it,
  * a context (unchanged) line keeps the attribution it already had,
  * a removed line drops out.

That is exactly blame semantics — a modified line reads as remove+add, so the new
text is credited to the change that wrote it. Metadata-only rows (move/retag)
carry no diff and don't affect line attribution.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from .diffing import _HUNK

_ATTR_KEYS = ("seq", "op", "source", "reason", "created_at")


def _apply_attributed(src: List[Tuple[str, dict]], patch: str,
                      attrib: dict) -> List[Tuple[str, dict]]:
    """Apply one diff to a list of (line_text, attribution) pairs. Added lines get
    `attrib`; context/removed lines are matched against src text exactly (the base
    is exact, so hunks line up — a mismatch is a real bug, surfaced loudly)."""
    out: List[Tuple[str, dict]] = []
    i = 0
    lines = patch.split("\n")
    n = len(lines)
    p = 0

    def marker_next(idx: int) -> bool:
        return idx + 1 < n and lines[idx + 1].startswith("\\ No newline")

    while p < n:
        line = lines[p]
        if line.startswith(("--- ", "+++ ")):
            p += 1
            continue
        m = _HUNK.match(line)
        if not m:
            p += 1
            continue
        old_start = int(m.group(1))
        target = old_start - 1 if old_start > 0 else 0
        out.extend(src[i:target])
        i = target
        p += 1
        while p < n and not lines[p].startswith("@@"):
            hl = lines[p]
            if hl.startswith("\\ No newline") or hl.startswith(("--- ", "+++ ")) or hl == "":
                p += 1
                continue
            tag, text = hl[0], hl[1:]
            if tag == " ":
                out.append(src[i]); i += 1
            elif tag == "-":
                i += 1
            elif tag == "+":
                out.append((text if marker_next(p) else text + "\n", attrib))
            p += 1
    out.extend(src[i:])
    return out


def replay(history: List[dict], upto_seq: Optional[int] = None) -> List[Tuple[str, dict]]:
    """Forward-replay history (ordered by seq) into a list of (line, attribution).
    `upto_seq` stops after that version (default: all → current body)."""
    lines: List[Tuple[str, dict]] = []
    for row in history:
        if upto_seq is not None and row["seq"] > upto_seq:
            break
        diff = row.get("diff")
        if not diff:
            continue  # metadata-only change: no line touched
        attrib = {k: row.get(k) for k in _ATTR_KEYS}
        lines = _apply_attributed(lines, diff, attrib)
    return lines


def reconstruct(history: List[dict], upto_seq: Optional[int] = None) -> str:
    """The exact body text at a version (default current)."""
    return "".join(text for text, _ in replay(history, upto_seq))


def annotate(history: List[dict], upto_seq: Optional[int] = None,
             lines: Optional[Iterable[int]] = None) -> List[dict]:
    """Blame view: one entry per line of the (reconstructed) body, each tagged
    with the change that last wrote it.

    Attribution always needs the full replay, but `lines` (an iterable of 1-based
    line numbers) restricts the *returned* entries — so you can blame one line or
    a slice without shipping the whole document. Out-of-range numbers are ignored.
    """
    want = set(lines) if lines is not None else None
    result = []
    for lineno, (text, attrib) in enumerate(replay(history, upto_seq), start=1):
        if want is not None and lineno not in want:
            continue
        result.append({"line": lineno, "text": text, **attrib})
    return result


def annotate_grouped(history: List[dict], upto_seq: Optional[int] = None,
                     include_text: bool = True) -> List[dict]:
    """Blame as runs: consecutive lines with the same attribution collapse into one
    block ``{start, end, lines, seq, op, source, reason, created_at[, text]}``.

    A 5000-line document edited by two authors returns a handful of blocks, not
    5000 rows. Set ``include_text=False`` for a pure ownership map (ranges only,
    no body) — tiny even for huge documents.
    """
    blocks: List[dict] = []
    cur: Optional[dict] = None
    cur_texts: List[str] = []
    for lineno, (text, attrib) in enumerate(replay(history, upto_seq), start=1):
        key = (attrib.get("seq"), attrib.get("source"), attrib.get("reason"),
               attrib.get("op"))
        if cur is not None and key == cur["_key"]:
            cur["end"] = lineno
            cur["lines"] += 1
            cur_texts.append(text)
        else:
            if cur is not None:
                _finish_block(cur, cur_texts, include_text, blocks)
            cur = {"_key": key, "start": lineno, "end": lineno, "lines": 1, **attrib}
            cur_texts = [text]
    if cur is not None:
        _finish_block(cur, cur_texts, include_text, blocks)
    return blocks


def _finish_block(block: dict, texts: List[str], include_text: bool,
                  out: List[dict]) -> None:
    block.pop("_key", None)
    if include_text:
        block["text"] = "".join(texts)
    out.append(block)
