"""Optional Qdrant vector backend (MEMGRES_VECTOR_BACKEND=qdrant).

pgvector keeps chunk vectors in the same Postgres and is the default. Qdrant is
for when you already run it or want a dedicated ANN service. The split of labor:

  * Qdrant holds the CHUNK vectors + a small payload (memory_id, offsets,
    namespace, src_hash) and ranks by cosine similarity, grouping to the best
    chunk per memory.
  * Postgres remains the source of truth for bodies and for every other filter
    (tags, subtree, expiry) — semantic recall ranks in Qdrant, then fetches and
    filters the candidate memories in Postgres.

So tag/tree/TTL changes never touch Qdrant — only a body change re-embeds and
`forget` deletes the chunks. Config for the connection:

  QDRANT_URL (default http://localhost:6333) · QDRANT_API_KEY ·
  MEMGRES_QDRANT_COLLECTION (default "memgres") ·
  MEMGRES_QDRANT_CA (path to a CA/self-signed cert to trust for an https URL)

Chunk points live in the ``{collection}_segments`` collection. (A pre-0.4
deployment also has a ``{collection}`` doc-vector collection; it is no longer
used — drop it once you've upgraded.)

MEMGRES_QDRANT_CA is only needed when Qdrant serves TLS with a certificate the
system trust store doesn't already know (a self-signed or private-CA deployment):
point it at the PEM to verify against. Leave it unset for plain http or a
publicly-trusted cert.

Needs the `[qdrant]` extra (qdrant-client).
"""

from __future__ import annotations

import os
import uuid
from typing import List, Optional, Sequence, Tuple

from .base import Hit, as_namespaces, grouped_chunk_search


class QdrantBackend:
    def __init__(self, dim: int, url: Optional[str] = None,
                 api_key: Optional[str] = None, collection: Optional[str] = None,
                 ca_cert: Optional[str] = None):
        from qdrant_client import QdrantClient

        self.dim = dim
        base = collection or os.environ.get("MEMGRES_QDRANT_COLLECTION", "memgres")
        # Chunk vectors live in a dedicated collection.
        self.chunks = f"{base}_segments"
        url = url or os.environ.get("QDRANT_URL", "http://localhost:6333")
        api_key = api_key or os.environ.get("QDRANT_API_KEY") or None
        ca_cert = ca_cert or os.environ.get("MEMGRES_QDRANT_CA") or None
        # A self-signed / private-CA https endpoint isn't in the system trust store;
        # `verify=<pem>` makes the underlying httpx client trust exactly that cert.
        extra = {"verify": ca_cert} if ca_cert else {}
        self.client = QdrantClient(url=url, api_key=api_key, **extra)
        self._ensure()

    @staticmethod
    def drop_chunks_collection() -> None:
        """Delete the chunk collection outright (used by re-embed before rebuilding
        at a new model dimension). Built WITHOUT ``_ensure`` so it doesn't trip the
        dim check on the old collection it's about to remove."""
        from qdrant_client import QdrantClient

        base = os.environ.get("MEMGRES_QDRANT_COLLECTION", "memgres")
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        api_key = os.environ.get("QDRANT_API_KEY") or None
        ca_cert = os.environ.get("MEMGRES_QDRANT_CA") or None
        extra = {"verify": ca_cert} if ca_cert else {}
        client = QdrantClient(url=url, api_key=api_key, **extra)
        coll = f"{base}_segments"
        if client.collection_exists(coll):
            client.delete_collection(coll)

    def _ensure(self) -> None:
        """The chunk collection (cosine, model dim) with keyword payload indexes on
        `memory_id` (for grouping + per-memory replace) and `namespace` (for the
        tenant filter that rides every recall). Hard-fail on a dim mismatch."""
        from qdrant_client.models import (Distance, PayloadSchemaType,
                                          VectorParams)

        if not self.client.collection_exists(self.chunks):
            self.client.create_collection(
                self.chunks,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )
        else:
            info = self.client.get_collection(self.chunks)
            size = info.config.params.vectors.size
            if size != self.dim:
                from ..schema import SchemaMismatch
                raise SchemaMismatch(
                    f"Qdrant collection '{self.chunks}' has dim {size}, model emits "
                    f"{self.dim} — re-embed into a fresh collection, don't mix models."
                )
        info = self.client.get_collection(self.chunks)
        schema = info.payload_schema or {}
        for field in ("memory_id", "namespace"):
            if field not in schema:
                self.client.create_payload_index(
                    self.chunks, field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD,
                )

    # ─── point helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _chunk_point_id(memory_id: str, seq: int) -> str:
        """Deterministic, collision-free point id per (memory_id, seq). Qdrant
        needs uuid or int ids; a uuid5 of the pair is both."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{memory_id}:{seq}"))

    def _mem_filter(self, memory_id: str, ns: Optional[str] = None):
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        must = [FieldCondition(key="memory_id", match=MatchValue(value=memory_id))]
        if ns is not None:
            must.append(FieldCondition(key="namespace", match=MatchValue(value=ns)))
        return Filter(must=must)

    # ─── chunk store ──────────────────────────────────────────────────────────
    def index_chunks(self, conn, memory_id: str, ns: str, src_hash: str,
                     chunks: Sequence[Tuple[int, int, int, Sequence[float]]]
                     ) -> None:
        from qdrant_client.models import FilterSelector, PointStruct

        # Replace-all: drop this memory's existing chunk points, then insert.
        self.client.delete(self.chunks,
                           points_selector=FilterSelector(filter=self._mem_filter(memory_id)))
        points = [
            PointStruct(
                id=self._chunk_point_id(memory_id, seq),
                vector=list(vec),
                payload={"memory_id": memory_id, "seq": seq, "seg_start": s,
                         "seg_end": e, "namespace": ns, "src_hash": src_hash},
            )
            for (seq, s, e, vec) in chunks
        ]
        if points:
            self.client.upsert(self.chunks, points=points)

    def delete_chunks(self, conn, memory_id: str, ns: str) -> None:
        from qdrant_client.models import FilterSelector
        self.client.delete(self.chunks,
                           points_selector=FilterSelector(filter=self._mem_filter(memory_id)))

    def chunk_src_hash(self, conn, memory_id: str, ns: str) -> Optional[str]:
        # ns-scoped: a tenant never reads another's chunk metadata, mirroring the
        # pgvector query's (memory_id, namespace) filter (defense-in-depth).
        points, _ = self.client.scroll(
            self.chunks, scroll_filter=self._mem_filter(memory_id, ns),
            limit=1, with_payload=True, with_vectors=False,
        )
        if not points:
            return None
        return (points[0].payload or {}).get("src_hash")

    def retag_namespace(self, conn, old_ns: str, new_ns: str) -> int:
        """Rewrite the namespace payload on every chunk point of ``old_ns``.

        Qdrant is out of band, so this cannot join the Postgres transaction. It
        is therefore done FIRST and the memories move second: a run that dies in
        between leaves points tagged for a namespace whose memories are still
        orphaned, and orphaned memories are unreachable anyway — so the halfway
        state answers nothing wrong, and re-running finishes the job."""
        from qdrant_client.models import (FieldCondition, Filter, MatchValue)

        flt = Filter(must=[FieldCondition(key="namespace",
                                          match=MatchValue(value=old_ns))])
        moved, offset = 0, None
        while True:
            points, offset = self.client.scroll(
                self.chunks, scroll_filter=flt, limit=256,
                with_payload=False, with_vectors=False, offset=offset)
            if not points:
                break
            # wait=True because `adopt_orphans` moves the vectors FIRST and the
            # rows second, and that ordering only protects anything if the
            # vector write is durable before the rows follow.
            self.client.set_payload(self.chunks, payload={"namespace": new_ns},
                                    points=[p.id for p in points], wait=True)
            moved += len(points)
            if offset is None:
                break
        return moved

    # ─── grouped semantic ranking ─────────────────────────────────────────────
    def search(self, conn, cfg, query_vec: Sequence[float], k: int, ns,
               tags: Optional[Sequence[str]], path_prefix: Optional[str],
               tags_match: str = "all") -> List[Hit]:
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        # MatchAny over the same normalized set Postgres filters on. A recall may
        # span several namespaces; `grouped_chunk_search` re-checks every candidate
        # against Postgres, so this filter governs recall quality, not tenancy.
        spaces = as_namespaces(ns)

        def fetch_chunks(overfetch: int, exclude: List[str]):
            must = [FieldCondition(key="namespace", match=MatchAny(any=spaces))]
            must_not = ([FieldCondition(key="memory_id", match=MatchAny(any=exclude))]
                        if exclude else None)
            flt = Filter(must=must, must_not=must_not)
            # Native grouping: one best chunk per memory, so `overfetch` groups are
            # `overfetch` DISTINCT memories already.
            res = self.client.query_points_groups(
                self.chunks, query=list(query_vec), group_by="memory_id",
                group_size=1, limit=overfetch, query_filter=flt, with_payload=True,
            )
            out = []
            for g in res.groups:
                if not g.hits:
                    continue
                p = g.hits[0]
                pl = p.payload or {}
                out.append((str(g.id), int(pl["seg_start"]), int(pl["seg_end"]),
                            float(p.score)))
            return out

        return grouped_chunk_search(conn, ns, k, tags, path_prefix, fetch_chunks,
                                    tags_match)
