"""Optional Qdrant vector backend (MEMGRES_VECTOR_BACKEND=qdrant).

pgvector keeps vectors in the same Postgres and is the default. Qdrant is for
when you already run it or want a dedicated ANN service. The split of labor:

  * Qdrant holds only the vector + the `namespace` (so ranking never crosses
    tenants) and ranks by cosine similarity.
  * Postgres remains the source of truth for bodies and for every other filter
    (tags, subtree, expiry) — semantic recall ranks in Qdrant, then fetches and
    filters the candidates in Postgres.

So tag/tree/TTL changes never need to touch Qdrant — only a body change re-embeds
(upsert) and `forget` deletes the point. Config for the connection:

  QDRANT_URL (default http://localhost:6333) · QDRANT_API_KEY ·
  MEMGRES_QDRANT_COLLECTION (default "memgres") ·
  MEMGRES_QDRANT_CA (path to a CA/self-signed cert to trust for an https URL)

MEMGRES_QDRANT_CA is only needed when Qdrant serves TLS with a certificate the
system trust store doesn't already know (a self-signed or private-CA deployment):
point it at the PEM to verify against. Leave it unset for plain http or a
publicly-trusted cert.

Needs the `[qdrant]` extra (qdrant-client).
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence, Tuple

from .base import Hit, build_filters


class QdrantBackend:
    def __init__(self, dim: int, url: Optional[str] = None,
                 api_key: Optional[str] = None, collection: Optional[str] = None,
                 ca_cert: Optional[str] = None):
        from qdrant_client import QdrantClient

        self.dim = dim
        self.collection = collection or os.environ.get("MEMGRES_QDRANT_COLLECTION", "memgres")
        url = url or os.environ.get("QDRANT_URL", "http://localhost:6333")
        api_key = api_key or os.environ.get("QDRANT_API_KEY") or None
        ca_cert = ca_cert or os.environ.get("MEMGRES_QDRANT_CA") or None
        # A self-signed / private-CA https endpoint isn't in the system trust store;
        # `verify=<pem>` makes the underlying httpx client trust exactly that cert.
        extra = {"verify": ca_cert} if ca_cert else {}
        self.client = QdrantClient(url=url, api_key=api_key, **extra)
        self._ensure()

    def _ensure(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )
            self._ensure_namespace_index()
            return
        info = self.client.get_collection(self.collection)
        size = info.config.params.vectors.size
        if size != self.dim:
            from ..schema import SchemaMismatch
            raise SchemaMismatch(
                f"Qdrant collection '{self.collection}' has dim {size}, model emits "
                f"{self.dim} — re-embed into a fresh collection, don't mix models."
            )
        self._ensure_namespace_index()

    def _ensure_namespace_index(self) -> None:
        """Keyword payload index on `namespace` so tenant-filtered ANN search
        stays fast as the collection grows (isolation is enforced by the query
        filter regardless; this is purely the speed of that filter). Idempotent:
        check the existing payload schema rather than catch a re-create error."""
        from qdrant_client.models import PayloadSchemaType

        info = self.client.get_collection(self.collection)
        if "namespace" in (info.payload_schema or {}):
            return
        self.client.create_payload_index(
            self.collection, field_name="namespace",
            field_schema=PayloadSchemaType.KEYWORD,
        )

    # ─── point ops ──────────────────────────────────────────────────────────
    def upsert(self, id: str, vector: Sequence[float], namespace: str) -> None:
        from qdrant_client.models import PointStruct

        self.client.upsert(
            self.collection,
            points=[PointStruct(id=id, vector=list(vector),
                                payload={"namespace": namespace})],
        )

    def delete(self, id: str) -> None:
        from qdrant_client.models import PointIdsList

        self.client.delete(self.collection, points_selector=PointIdsList(points=[id]))

    def query(self, vector: Sequence[float], k: int,
              namespace: str) -> List[Tuple[str, float]]:
        """Top-k point ids by cosine similarity, scoped to one namespace."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        flt = Filter(must=[FieldCondition(key="namespace",
                                          match=MatchValue(value=namespace))])
        res = self.client.query_points(
            self.collection, query=list(vector), limit=k,
            query_filter=flt, with_payload=False,
        ).points
        return [(str(p.id), float(p.score)) for p in res]

    # ─── VectorBackend interface (conn is unused: vectors are out-of-band) ────
    def upsert_doc(self, conn, id: str, vec: Sequence[float], ns: str) -> None:
        self.upsert(id, vec, ns)

    def delete_doc(self, conn, id: str, ns: str) -> None:
        self.delete(id)

    def search(self, conn, cfg, query_vec: Sequence[float], k: int, ns: str,
               tags: Optional[Sequence[str]], path_prefix: Optional[str]) -> List[Hit]:
        """Rank in Qdrant (namespace-scoped), then fetch + filter bodies in Postgres.
        Over-fetch when tag/subtree filters apply, since those are enforced in PG."""
        overfetch = k if not (tags or path_prefix) else min(max(k * 10, k), 500)
        pairs = self.query(query_vec, overfetch, ns)     # [(id, score)] cosine similarity
        if not pairs:
            return []
        score = {pid: s for pid, s in pairs}
        where, params = build_filters(ns, tags, path_prefix)  # ns, expiry, tags, subtree
        sql = (f"SELECT id, body, tags, path::text FROM memory "
               f"WHERE {where} AND id = ANY(%s)")
        with conn.cursor() as cur:
            cur.execute(sql, params + [list(score.keys())])
            hits = [Hit(str(r[0]), r[1], list(r[2]), r[3], score[str(r[0])])
                    for r in cur.fetchall()]
        hits.sort(key=lambda h: h.score, reverse=True)   # Qdrant order, minus PG-filtered
        return hits[:k]
