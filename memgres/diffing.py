"""Content hashing and unified-diff make/apply.

A write can arrive two ways: the whole new body, or a *diff* (a unified diff, the
same text ``git diff`` prints). Either way we store a unified diff in history, so
every change is replayable and attributable.

Concurrency is guarded by content hash, not locks: the caller passes the hash of
the body it started from; the store applies the change only if the current body
still hashes to that value (optimistic locking). Because the base is thus exact,
diff application never needs fuzz — a hunk that does not line up is a real
conflict and raises, rather than corrupting the body silently.

Trailing newlines are preserved exactly via the ``\\ No newline at end of file``
marker, the way git does it; without it a body that does/doesn't end in a
newline would not survive a make→apply round-trip.
"""

from __future__ import annotations

import difflib
import hashlib
import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_NO_NL = "\\ No newline at end of file"


def content_hash(body: str) -> str:
    """Stable identifier of a body's exact content. Used for optimistic
    locking and as the link in the history hash-chain."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def make_diff(old: str, new: str) -> str:
    """Unified diff turning ``old`` into ``new``. Empty string if identical."""
    if old == new:
        return ""
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    out: list[str] = []
    for line in difflib.unified_diff(a, b, lineterm=""):
        if line.startswith(("--- ", "+++ ", "@@")):
            out.append(line + "\n")
        elif line.endswith("\n"):
            out.append(line)
        else:  # a content line with no trailing newline = last line of file
            out.append(line + "\n")
            out.append(_NO_NL + "\n")
    return "".join(out)


class DiffConflict(Exception):
    """A patch hunk did not match the body it was applied to."""


def apply_diff(old: str, patch: str) -> str:
    """Apply a unified diff to ``old`` and return the new body.

    Raises DiffConflict if a hunk's context/removed lines do not match — with
    an exact base (enforced by the hash check upstream) this only happens on a
    malformed or stale patch, never as normal fuzz.
    """
    if patch.strip() == "":
        return old

    src = old.splitlines(keepends=True)
    out: list[str] = []
    i = 0                       # index into src (0-based)
    lines = patch.split("\n")
    n = len(lines)
    p = 0
    hunks = 0                   # guard: a non-empty patch must apply ≥1 hunk

    def marker_next(idx: int) -> bool:
        return idx + 1 < n and lines[idx + 1].startswith("\\ No newline")

    while p < n:
        line = lines[p]
        if line.startswith("--- ") or line.startswith("+++ "):
            p += 1
            continue
        m = _HUNK.match(line)
        if not m:
            # A `@@`-line that isn't a valid header is a malformed hunk, not a
            # skippable stray — reject it rather than silently dropping the edit.
            if line.startswith("@@"):
                raise DiffConflict(f"malformed hunk header: {line!r}")
            p += 1
            continue
        hunks += 1

        old_start = int(m.group(1))
        # @@ lines are 1-based; a hunk adding to an empty side uses -0 → 0.
        target = old_start - 1 if old_start > 0 else 0
        if target < i:
            raise DiffConflict(f"hunk out of order at source line {old_start}")
        out.extend(src[i:target])
        i = target
        p += 1

        while p < n and not lines[p].startswith("@@"):
            hl = lines[p]
            if hl.startswith("\\ No newline") or hl.startswith(("--- ", "+++ ")):
                p += 1
                continue
            if hl == "":                    # trailing split artifact
                p += 1
                continue
            tag, text = hl[0], hl[1:]
            if tag == " ":                  # context: copy exact source bytes
                _expect(src, i, text)
                out.append(src[i]); i += 1
            elif tag == "-":                # removal: verify then drop
                _expect(src, i, text)
                i += 1
            elif tag == "+":                # addition: marker decides newline
                out.append(text if marker_next(p) else text + "\n")
            else:
                raise DiffConflict(f"unexpected patch line: {hl!r}")
            p += 1

    out.extend(src[i:])
    # A non-empty patch that matched no hunk header applied nothing — that is a
    # malformed / non-unified-diff payload, not a legitimate no-op. Fail loudly
    # instead of silently returning the body unchanged.
    if hunks == 0:
        raise DiffConflict("patch contains no @@ hunk — malformed or not a unified diff")
    return "".join(out)


def _expect(src: list[str], i: int, text: str) -> None:
    if i >= len(src):
        raise DiffConflict(f"patch expects a line past end of body: {text!r}")
    if src[i].rstrip("\n") != text.rstrip("\n"):
        raise DiffConflict(f"context mismatch at line {i + 1}: "
                           f"have {src[i]!r}, patch expects {text!r}")


def byte_len(text: str) -> int:
    """UTF-8 byte length — what limits are measured in."""
    return len(text.encode("utf-8"))
