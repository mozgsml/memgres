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


# ─── byte length is measured in UTF-8, not characters ────────────────────
def test_byte_len_utf8():
    assert byte_len("abc") == 3
    assert byte_len("ж") == 2          # cyrillic = 2 bytes
    assert byte_len("😀") == 4          # emoji = 4 bytes


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
