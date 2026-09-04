"""
Backend registry — the one place mapping a KnowledgeBase's `backend` value to
the machinery that serves it.

Ingest and search lists are separate because they are not the same question:
a hybrid KB ingests through both mechanisms, while a raw KB ingests alone but
has *no* search backends at all (its documents are reached by reading, which
is not a search).
"""
from __future__ import annotations

from typing import List

from .base import RetrievalBackend
from .fulltext import FullTextBackend
from .hybrid import HybridBackend
from .raw import RawBackend
from .vector import VectorBackend

__all__ = [
    'FullTextBackend',
    'HybridBackend',
    'RawBackend',
    'RetrievalBackend',
    'VectorBackend',
    'get_ingest_backends',
    'get_search_backends',
]

_INGEST: dict = {
    'vector': [VectorBackend],
    'fulltext': [FullTextBackend],
    'raw': [RawBackend],
    'hybrid': [HybridBackend],
}

_SEARCH: dict = {
    'vector': [VectorBackend],
    'fulltext': [FullTextBackend],
    'raw': [],
    'hybrid': [HybridBackend],
}


def _build(kb, classes) -> List[RetrievalBackend]:
    return [cls(kb) for cls in classes]


def get_ingest_backends(kb) -> List[RetrievalBackend]:
    """Backends a document upload into this KB must pass through."""
    return _build(kb, _INGEST.get(getattr(kb, 'backend', 'vector'), []))


def get_search_backends(kb) -> List[RetrievalBackend]:
    """Backends that can answer a query against this KB ([] for raw)."""
    return _build(kb, _SEARCH.get(getattr(kb, 'backend', 'vector'), []))
