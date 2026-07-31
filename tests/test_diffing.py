"""Pure-logic tests for the diff/version engine (no database needed)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memgres.diffing import (  # noqa: E402
    DiffConflict, apply_diff, byte_len, content_hash, make_diff,
)


# ─── content_hash ────────────────────────────────────────────────────────
def test_hash_stable_and_distinct():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


# ─── round-trip: make then apply reproduces the target ───────────────────
CASES = [
    ("", "hello"),                                   # create from empty
    ("hello", ""),                                   # delete all
    ("line one\nline two\nline three\n",
     "line one\nline TWO changed\nline three\n"),    # middle edit
    ("a\nb\nc\n", "a\nb\nc\nd\ne\n"),                 # append
    ("a\nb\nc\nd\n", "a\nd\n"),                       # remove middle
    ("no trailing newline", "no trailing newline!"),  # no final \n
    ("first\nsecond", "zero\nfirst\nsecond"),        # prepend, no final \n
    ("同じ\n行\n", "同じ\n別の行\n"),                    # unicode
]


@pytest.mark.parametrize("old,new", CASES)
def test_make_apply_roundtrip(old, new):
    patch = make_diff(old, new)
    assert apply_diff(old, patch) == new


def test_identical_bodies_make_empty_diff():
    assert make_diff("same\n", "same\n") == ""
    assert apply_diff("same\n", "") == "same\n"


# ─── conflict detection: a patch against the wrong base must not corrupt ──
def test_apply_to_changed_base_raises():
    old = "alpha\nbeta\ngamma\n"
    patch = make_diff(old, "alpha\nBETA\ngamma\n")
    # someone else already changed the body out from under this patch
    drifted = "alpha\nbeta CHANGED\ngamma\n"
    with pytest.raises(DiffConflict):
        apply_diff(drifted, patch)


# ─── malformed patches must fail loudly, never silently no-op ────────────
def test_malformed_hunk_header_raises():
    # A bare `@@` (no `-a,b +c,d` line spec) is not a valid hunk header. It used
    # to be silently skipped, so the whole patch applied nothing and the body
    # came back unchanged — a silent no-op. Now it raises.
    bad = "@@\n-old line\n+new line\n"
    with pytest.raises(DiffConflict):
        apply_diff("old line\n", bad)


def test_patch_without_any_hunk_raises():
    # Non-empty payload that contains no @@ hunk at all applied nothing → raise,
    # rather than returning the source body untouched.
    with pytest.raises(DiffConflict):
        apply_diff("body\n", "-body\n+new\n")


def test_empty_patch_is_still_a_noop():
    # A genuinely empty patch is the one legitimate no-op (unchanged behavior).
    assert apply_diff("keep\n", "") == "keep\n"
    assert apply_diff("keep\n", "   \n") == "keep\n"


# ─── history row_hash stays backward-compatible when there's no author ────
def test_row_hash_unchanged_without_author():
    # A history row with no author must hash IDENTICALLY to the pre-authorship
    # formula, so hash-chains written before history_author still verify after
    # the upgrade. Single-mode and global-admin writes are exactly this case.
    import hashlib

    from memgres.store import _row_hash

    parts = ["", "m", "1", "create", "d", "h", "", "t", "s", "r"]
    legacy = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    assert _row_hash(None, "m", 1, "create", "d", "h", None, ["t"], "s", "r") == legacy
    # None author is the same as omitting it
    assert _row_hash(None, "m", 1, "create", "d", "h", None, ["t"], "s", "r",
                     None, None) == legacy


def test_row_hash_folds_author_when_present():
    # An author, once present, changes the row hash — so tampering that strips or
    # swaps authorship is detectable by verify_history.
    from memgres.store import _row_hash

    base = _row_hash(None, "m", 1, "create", "d", "h", None, ["t"], "s", "r")
    with_author = _row_hash(None, "m", 1, "create", "d", "h", None, ["t"], "s", "r",
                            "user-x", "tok-y")
    assert with_author != base
    # the token id participates too: same user, different token → different hash
    other_tok = _row_hash(None, "m", 1, "create", "d", "h", None, ["t"], "s", "r",
                          "user-x", "tok-z")
    assert other_tok != with_author


def test_author_cannot_be_forged_via_delimiter_in_reason():
    # Regression (security review #1): author is folded through a domain-separated
    # outer hash, NOT appended as more \x1f-joined fields. So an authored row must
    # NOT collide with an author-less row whose client-controlled `reason` is
    # crafted to smuggle the author tuple across the field boundary.
    from memgres.store import _row_hash

    authored = _row_hash(None, "m", 1, "create", "d", "h", None, ["t"],
                         "src", "why", "user-x", "tok-y")
    # attacker moves "\x1f user-x \x1f tok-y" into reason and NULLs the author
    forged = _row_hash(None, "m", 1, "create", "d", "h", None, ["t"],
                       "src", "why\x1fuser-x\x1ftok-y", None, None)
    assert authored != forged


# ─── byte length is measured in UTF-8, not characters ────────────────────
def test_byte_len_utf8():
    assert byte_len("abc") == 3
    assert byte_len("ж") == 2          # cyrillic = 2 bytes
    assert byte_len("😀") == 4          # emoji = 4 bytes


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
