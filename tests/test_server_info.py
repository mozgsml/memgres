"""server_info: effective limits + capabilities, no secrets.

Pure config-derived logic — no database needed.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memgres.config import load  # noqa: E402
from memgres.info import server_info  # noqa: E402


def _clear(monkeypatch):
    import os
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)


def test_top_level_keys_and_limits(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MEMGRES_MAX_BODY_BYTES", "9000")
    monkeypatch.setenv("MEMGRES_MAX_WRITE_BYTES", "800")
    monkeypatch.setenv("MEMGRES_MAX_SOURCE_BYTES", "300")
    monkeypatch.setenv("MEMGRES_MAX_REASON_BYTES", "200")
    info = server_info(load())
    assert set(info) == {"version", "schema_version", "limits", "embed",
                         "recall_modes", "vector_backend", "key_mode", "fts_language"}
    assert info["limits"] == {"max_body_bytes": 9000, "max_write_bytes": 800,
                              "max_source_bytes": 300, "max_reason_bytes": 200}


def test_reports_version_and_schema_version(monkeypatch):
    # server_info must expose the running version (so a client can tell what it's
    # talking to) and the DB schema version this build migrates to.
    _clear(monkeypatch)
    import memgres
    from memgres.schema import SCHEMA_VERSION
    info = server_info(load())
    assert info["version"] == memgres.__version__      # from code, not stale metadata
    assert isinstance(info["version"], str) and info["version"]
    assert info["schema_version"] == SCHEMA_VERSION


def test_recall_modes_lexical_when_no_embedder(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "none")
    info = server_info(load())
    assert info["recall_modes"] == ["lexical"]
    assert info["embed"]["provider"] == "none"
    assert info["embed"]["model"] is None


def test_recall_modes_semantic_when_embedder_configured(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "jina")
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", "jina-embeddings-v3")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", "1024")
    cfg = load()
    info = server_info(cfg, embed_dim=1024)
    assert info["recall_modes"] == ["lexical", "semantic", "hybrid", "auto"]
    assert info["embed"] == {"provider": "jina", "model": "jina-embeddings-v3",
                             "dim": 1024}


def test_dim_falls_back_to_config_when_no_live_embedder(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "jina")
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", "m")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", "512")
    info = server_info(load())          # no embed_dim passed
    assert info["embed"]["dim"] == 512


def test_no_secrets_leak(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MEMGRES_TOKEN", "mgk_supersecret_token")
    monkeypatch.setenv("MEMGRES_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("MEMGRES_EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("MEMGRES_EMBED_DIM", "1536")
    monkeypatch.setenv("MEMGRES_EMBED_API_KEY", "sk-secretkey123")
    monkeypatch.setenv("MEMGRES_DATABASE_URL",
                       "postgresql://user:pw@host:5432/db")
    info = server_info(load())
    blob = json.dumps(info)
    # no secret values anywhere in the output
    assert "mgk_supersecret_token" not in blob
    assert "sk-secretkey123" not in blob
    assert "postgresql://" not in blob
    # no secret keys anywhere in the (possibly nested) structure
    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    allk = set(keys(info))
    assert not (allk & {"token", "api_key", "embed_api_key", "database_url"})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
