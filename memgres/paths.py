"""What a tree path is — in ONE place.

The shape was written down twice before, in the link parser and in whatever SQL
happened to cast a string to `ltree`, and the two disagreed: a hyphen was legal
to store and illegal to link, so half the links in a real corpus silently
vanished (fixed in 0.12.0). This module exists so a third definition does not
appear.

It also gives the check a place to happen BEFORE the value reaches Postgres. An
unvalidated prefix arrived at the database as `%s::ltree` and came back as
`psycopg.errors.SyntaxError: ltree syntax error at character 1` — a message
about our schema, not about their input, and one the caller can do nothing with.
"""

from __future__ import annotations

import re
from typing import Optional

# Labels of word characters (letters in ANY script, digits, underscore) or
# hyphens, joined by dots.
#
# Two things had to be widened here, each because a real path was being called
# invalid. The hyphen: Postgres has allowed it in ltree labels since 13, the
# minimum this project supports. Non-ASCII letters: `ops.тариф` stores and reads
# perfectly well, and an existing test says so — a check that rejected it would
# have made a working path unwritable, which is worse than the error it prevents.
#
# The parser for `[[links]]` uses the SAME alphabet on purpose. When it was
# narrower, every hyphenated link was dropped as prose; a narrower rule here
# would recreate that, only for Cyrillic.
PATH_RE = re.compile(r"^[\w-]+(\.[\w-]+)*$", re.UNICODE)


def is_path(value: Optional[str]) -> bool:
    return bool(value) and bool(PATH_RE.match(value))


def check_path(value: Optional[str], field: str = "path") -> Optional[str]:
    """Return `value` unchanged, or raise ValueError naming the field and the
    shape. Empty/None passes — "no path given" is not a malformed one."""
    if value is None or value == "":
        return value
    if not PATH_RE.match(value):
        raise ValueError(
            f"`{field}` is not a tree path: {value!r}. A path is labels of "
            f"letters (any script), digits, `_` or `-` joined by dots, like "
            f"`ops.memory.onboarding` — no spaces, slashes or leading dots.")
    return value
