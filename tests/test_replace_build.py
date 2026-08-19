"""build_replace(): a lone replace_old/replace_new is an error, never a silent
delete. Regression guard for meta.memgres.replace_new_dropped — a missing
replace_new used to be coerced to "" and delete the matched text on a success."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memgres.store import build_replace  # noqa: E402


def test_both_omitted_is_no_replace():
    assert build_replace(None, None) is None


def test_both_present_builds_tuple():
    assert build_replace("old", "new") == ("old", "new")


def test_explicit_empty_new_is_allowed_delete():
    # deleting the matched text on purpose stays possible — but ONLY explicitly
    assert build_replace("old", "") == ("old", "")


def test_lone_old_raises_not_silent_delete():
    # the bug: this used to become ("old", "") and silently delete `old`
    with pytest.raises(ValueError):
        build_replace("old", None)


def test_lone_new_raises():
    with pytest.raises(ValueError):
        build_replace(None, "new")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
