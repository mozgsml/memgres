"""Pluggable vector backends: pgvector (in-row) or Qdrant (out-of-band).

``store.py`` and ``search.py`` talk only to the :class:`VectorBackend` interface
and let :func:`make_backend` pick the concrete one from config.
"""

from .base import VectorBackend, Hit, build_filters, make_backend

__all__ = ["VectorBackend", "Hit", "build_filters", "make_backend"]
