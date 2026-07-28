"""Pluggable embedding providers, selected by env.

    MEMGRES_EMBED_PROVIDER = none | local | jina | openai

``none`` disables semantic recall entirely (lexical FTS still works, no model,
no API, no GPU). ``local`` runs a sentence-transformers model in-process.
``jina`` / ``openai`` call a hosted API. All are behind one tiny interface so
the store never cares which is configured::

    emb = get_embedder(cfg)          # -> Embedder | None
    if emb:
        vecs = emb.embed_documents(["…"])   # for writes
        q    = emb.embed_query("…")          # for recall
        dim  = emb.dim

The document/query split matters: several models (and Jina) want an explicit
"is this a passage or a search query?" hint, and mixing them up quietly hurts
recall. Cloud calls use stdlib urllib so the base install stays dependency-light
(only ``local`` pulls in sentence-transformers, via the ``[local]`` extra).
"""

from __future__ import annotations

import json
import urllib.request
from typing import List, Optional, Sequence

from .config import Config


class Embedder:
    """Interface: implementations return unit-normalized float lists."""

    dim: int

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> List[float]:
        raise NotImplementedError


# ─── local: sentence-transformers ────────────────────────────────────────────
class _LocalEmbedder(Embedder):
    def __init__(self, model_name: str, want_dim: int, max_seq: int = 0):
        from sentence_transformers import SentenceTransformer  # lazy: heavy import

        if not model_name:
            raise ValueError("MEMGRES_EMBED_MODEL is required for the local provider")
        self._model = SentenceTransformer(model_name, device="cpu")
        if max_seq > 0:  # override the model's context window (tokens)
            self._model.max_seq_length = max_seq
        self.dim = self._model.get_sentence_embedding_dimension()
        if want_dim and want_dim != self.dim:
            raise ValueError(
                f"MEMGRES_EMBED_DIM={want_dim} but '{model_name}' emits {self.dim}"
            )

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        vecs = self._model.encode(
            list(texts), normalize_embeddings=True, convert_to_numpy=True
        )
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]


# ─── cloud: Jina and OpenAI share an OpenAI-shaped /embeddings endpoint ───────
class _HttpEmbedder(Embedder):
    def __init__(self, model: str, dim: int, api_key: str, api_base: str,
                 query_task: Optional[str] = None, passage_task: Optional[str] = None,
                 require_key: bool = True):
        if not model:
            raise ValueError("MEMGRES_EMBED_MODEL is required for HTTP providers")
        if not api_base:
            raise ValueError("MEMGRES_EMBED_API_BASE (server URL) is required")
        if require_key and not api_key:
            raise ValueError("MEMGRES_EMBED_API_KEY is required for this provider")
        if dim <= 0:
            raise ValueError("MEMGRES_EMBED_DIM must be set for HTTP providers")
        self.dim = dim
        self._model = model
        self._key = api_key
        self._base = api_base.rstrip("/")
        self._query_task = query_task
        self._passage_task = passage_task

    def _post(self, inputs: Sequence[str], task: Optional[str]) -> List[List[float]]:
        payload = {"model": self._model, "input": list(inputs)}
        if task:  # Jina uses task to distinguish passage vs query; OpenAI ignores it
            payload["task"] = task
        headers = {"Content-Type": "application/json"}
        if self._key:  # local servers (LM Studio/Ollama) often need no key
            headers["Authorization"] = f"Bearer {self._key}"
        req = urllib.request.Request(
            f"{self._base}/embeddings",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
        rows = sorted(body["data"], key=lambda d: d.get("index", 0))
        out = [r["embedding"] for r in rows]
        for v in out:
            if len(v) != self.dim:
                raise ValueError(
                    f"provider returned dim {len(v)}, config says {self.dim} — "
                    f"fix MEMGRES_EMBED_DIM to match the model"
                )
        return out

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return self._post(texts, self._passage_task)

    def embed_query(self, text: str) -> List[float]:
        return self._post([text], self._query_task)[0]


def get_embedder(cfg: Config) -> Optional[Embedder]:
    """Build the embedder the config asks for, or None for lexical-only."""
    p = cfg.embed_provider
    if p == "none":
        return None
    if p == "local":
        return _LocalEmbedder(cfg.embed_model, cfg.embed_dim, cfg.embed_max_seq)
    if p == "jina":
        return _HttpEmbedder(
            cfg.embed_model, cfg.embed_dim, cfg.embed_api_key,
            cfg.embed_api_base or "https://api.jina.ai/v1",
            query_task="retrieval.query", passage_task="retrieval.passage",
        )
    if p == "openai":
        return _HttpEmbedder(
            cfg.embed_model, cfg.embed_dim, cfg.embed_api_key,
            cfg.embed_api_base or "https://api.openai.com/v1",
        )
    if p in ("openai-compatible", "compatible", "custom"):
        # any OpenAI-shaped /embeddings server: LM Studio, Ollama, vLLM, TEI,
        # LocalAI, … base URL required, key optional.
        return _HttpEmbedder(
            cfg.embed_model, cfg.embed_dim, cfg.embed_api_key, cfg.embed_api_base,
            require_key=False,
        )
    raise ValueError(f"unknown embed provider: {p}")  # config.validate guards this
