"""Unit test: MEMGRES_QDRANT_CA is forwarded to the Qdrant client as `verify=`.

No live Qdrant needed — a fake client records the kwargs it was built with and
stubs the calls `_ensure()` makes, so this runs anywhere (unlike the integration
tests that need a real server). Guards the self-signed / private-CA TLS path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


class _FakeCollectionInfo:
    def __init__(self, dim):
        class _V:  # info.config.params.vectors.size
            size = dim
        class _P:
            vectors = _V()
        class _C:
            params = _P()
        self.config = _C()
        self.payload_schema = {"namespace": object()}  # pretend the index exists


class _FakeClient:
    last_kwargs = None

    def __init__(self, **kwargs):
        _FakeClient.last_kwargs = kwargs

    def collection_exists(self, _c):
        return True

    def get_collection(self, _c):
        return _FakeCollectionInfo(dim=8)


@pytest.fixture
def fake_qdrant(monkeypatch):
    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient", _FakeClient)
    _FakeClient.last_kwargs = None
    yield


def test_ca_cert_forwarded_as_verify(fake_qdrant, monkeypatch):
    monkeypatch.setenv("MEMGRES_QDRANT_CA", "/etc/ssl/my-ca.pem")
    from memgres.qdrant_backend import QdrantIndex
    QdrantIndex(dim=8, url="https://qdrant.example:6333")
    assert _FakeClient.last_kwargs.get("verify") == "/etc/ssl/my-ca.pem"


def test_no_ca_means_no_verify_kwarg(fake_qdrant, monkeypatch):
    monkeypatch.delenv("MEMGRES_QDRANT_CA", raising=False)
    from memgres.qdrant_backend import QdrantIndex
    QdrantIndex(dim=8, url="http://qdrant.example:6333")
    # default trust store path: don't pin `verify` at all
    assert "verify" not in _FakeClient.last_kwargs


def test_explicit_ca_arg_beats_env(fake_qdrant, monkeypatch):
    monkeypatch.setenv("MEMGRES_QDRANT_CA", "/env/ca.pem")
    from memgres.qdrant_backend import QdrantIndex
    QdrantIndex(dim=8, url="https://qdrant.example:6333", ca_cert="/arg/ca.pem")
    assert _FakeClient.last_kwargs.get("verify") == "/arg/ca.pem"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
