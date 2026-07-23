"""Embedder factory dispatch and cloud-provider validation (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memgres.config import load  # noqa: E402
from memgres.embeddings import get_embedder, _HttpEmbedder  # noqa: E402


def _cfg(monkeypatch, **kw):
    for k in list(__import__("os").environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv("MEMGRES_" + k, v)
    return load()


def test_none_returns_no_embedder(monkeypatch):
    assert get_embedder(_cfg(monkeypatch, EMBED_PROVIDER="none")) is None


def test_cloud_requires_key(monkeypatch):
    cfg = _cfg(monkeypatch, EMBED_PROVIDER="jina", EMBED_MODEL="jina-embeddings-v3",
               EMBED_DIM="1024")
    with pytest.raises(ValueError, match="API_KEY"):
        get_embedder(cfg)


def test_cloud_requires_model(monkeypatch):
    cfg = _cfg(monkeypatch, EMBED_PROVIDER="openai", EMBED_DIM="1536",
               EMBED_API_KEY="sk-x")
    with pytest.raises(ValueError, match="EMBED_MODEL"):
        get_embedder(cfg)


def test_jina_defaults_to_jina_base(monkeypatch):
    cfg = _cfg(monkeypatch, EMBED_PROVIDER="jina", EMBED_MODEL="jina-embeddings-v3",
               EMBED_DIM="1024", EMBED_API_KEY="jina-x")
    emb = get_embedder(cfg)
    assert isinstance(emb, _HttpEmbedder)
    assert emb._base == "https://api.jina.ai/v1"
    assert emb._passage_task == "retrieval.passage"   # passage vs query hint
    assert emb.dim == 1024


def test_openai_base_and_no_task(monkeypatch):
    cfg = _cfg(monkeypatch, EMBED_PROVIDER="openai",
               EMBED_MODEL="text-embedding-3-small", EMBED_DIM="1536",
               EMBED_API_KEY="sk-x")
    emb = get_embedder(cfg)
    assert emb._base == "https://api.openai.com/v1"
    assert emb._passage_task is None                  # OpenAI has no task hint


def test_custom_api_base(monkeypatch):
    cfg = _cfg(monkeypatch, EMBED_PROVIDER="openai", EMBED_MODEL="m", EMBED_DIM="8",
               EMBED_API_KEY="k", EMBED_API_BASE="http://localhost:9999/v1/")
    emb = get_embedder(cfg)
    assert emb._base == "http://localhost:9999/v1"    # trailing slash trimmed


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
