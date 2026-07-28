"""Unit tests for the pure segmentation function (no DB, no embeddings)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memgres.segments import segment  # noqa: E402


def _covers(text, ranges):
    """Every character index in [0, len(text)) is inside at least one range,
    and no range steps outside the text."""
    n = len(text)
    covered = [False] * n
    for s, e in ranges:
        assert 0 <= s <= e <= n, f"range ({s},{e}) out of bounds for len {n}"
        for i in range(s, e):
            covered[i] = True
    return all(covered)


def test_empty_string():
    assert segment("") == []


def test_short_single_span():
    t = "Just one short sentence."
    assert segment(t, seg_chars=400) == [(0, len(t))]


def test_full_coverage_and_bounds():
    seg_chars, overlap = 120, 30
    t = ("Alpha beta gamma. " * 40).strip()  # well over seg_chars, sentence-y
    ranges = segment(t, seg_chars=seg_chars, overlap=overlap)
    assert _covers(t, ranges)
    # first range anchors at 0, last reaches the end (no gap at either edge)
    assert ranges[0][0] == 0 and ranges[-1][1] == len(t)
    # each segment bounded by seg_chars + the overlap slack
    for s, e in ranges:
        assert e - s <= seg_chars + overlap


def test_consecutive_segments_overlap():
    seg_chars, overlap = 100, 25
    t = ("Sentence number here. " * 30).strip()
    ranges = segment(t, seg_chars=seg_chars, overlap=overlap)
    assert len(ranges) >= 2
    # each later segment starts before the previous one ends → shared chars
    for (s0, e0), (s1, e1) in zip(ranges, ranges[1:]):
        assert s1 < e0, f"no overlap between {(s0, e0)} and {(s1, e1)}"


def test_unstructured_wall_is_covered_and_bounded():
    seg_chars, overlap = 80, 20
    t = "x" * 500  # no .!? or newlines: one oversized primary unit
    ranges = segment(t, seg_chars=seg_chars, overlap=overlap)
    assert _covers(t, ranges)
    assert len(ranges) > 1
    for s, e in ranges:
        assert e - s <= seg_chars + overlap


def test_unicode_offsets_are_code_points():
    seg_chars, overlap = 60, 15
    # Russian prose + emoji; offsets must be str indices, not bytes
    t = ("Привет мир. Это тест сегментации. " * 12 + "Эмодзи 🌍🚀🔥 внутри текста. ") * 2
    ranges = segment(t, seg_chars=seg_chars, overlap=overlap)
    assert _covers(t, ranges)
    # reconstruct via the offsets: every char reachable, slices are valid
    reconstructed = [False] * len(t)
    for s, e in ranges:
        chunk = t[s:e]                 # must not raise / mis-slice
        assert chunk == t[s:e]
        for i in range(s, e):
            reconstructed[i] = True
    assert all(reconstructed)
    # spot-check the emoji survives intact somewhere in the coverage
    assert any("🌍🚀🔥" in t[s:e] for s, e in ranges)


def test_overlap_clamped_below_seg_chars():
    t = "word " * 200
    # overlap >= seg_chars must not hang or break coverage (clamped internally)
    ranges = segment(t, seg_chars=50, overlap=999)
    assert _covers(t, ranges)


def test_tiny_text_one_span():
    t = "hi"
    assert segment(t, seg_chars=400) == [(0, 2)]
