"""Single source of truth for the package version.

Read at runtime by ``server_info`` (so an editable/dev checkout reports the code
it is actually running, not stale install metadata) and at build time by
``pyproject.toml`` (``[tool.setuptools.dynamic] version = {attr = ...}``). Bump
here at release; nowhere else carries the number.

PEP 440: a ``.devN`` suffix marks an unreleased build ahead of the last tag.
"""

__version__ = "0.4.1"
