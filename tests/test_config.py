"""Config is read fresh from env on each load() and validates limits."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memgres.config import load  # noqa: E402


def test_defaults(monkeypatch):
    for k in list(sys.modules):  # no leftover env assumptions
        pass
    monkeypatch.delenv("MEMGRES_MAX_BODY_BYTES", raising=False)
    monkeypatch.delenv("MEMGRES_MAX_WRITE_BYTES", raising=False)
    monkeypatch.delenv("MEMGRES_EMBED_PROVIDER", raising=False)
    c = load()
    assert c.max_body_bytes == 262_144
    assert c.max_write_bytes == 16_384
    assert c.key_mode == "single"
    assert c.embed_provider == "none"


def test_env_read_fresh_each_call(monkeypatch):
    monkeypatch.setenv("MEMGRES_MAX_WRITE_BYTES", "1000")  # keep write <= body
    monkeypatch.setenv("MEMGRES_MAX_BODY_BYTES", "5000")
    assert load().max_body_bytes == 5000
    monkeypatch.setenv("MEMGRES_MAX_BODY_BYTES", "6000")
    assert load().max_body_bytes == 6000     # not frozen at import


def test_write_larger_than_body_rejected(monkeypatch):
    monkeypatch.setenv("MEMGRES_MAX_BODY_BYTES", "1000")
    monkeypatch.setenv("MEMGRES_MAX_WRITE_BYTES", "9999")
    with pytest.raises(ValueError, match="MAX_WRITE_BYTES"):
        load()


def test_semantic_without_dim_rejected(monkeypatch):
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "jina")
    monkeypatch.setenv("MEMGRES_VECTOR_BACKEND", "pgvector")
    monkeypatch.delenv("MEMGRES_EMBED_DIM", raising=False)
    with pytest.raises(ValueError, match="dimension"):
        load()


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "banana")
    with pytest.raises(ValueError, match="EMBED_PROVIDER"):
        load()


def test_tree_default_on(monkeypatch):
    monkeypatch.delenv("MEMGRES_TREE", raising=False)
    assert load().tree_enabled is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
