"""
Inference Engine — Persistent HNSW Knowledge Base

Key design decisions:
- Embedding model: API-backed, OpenAI-compatible /embeddings endpoint
  (default nvidia/nemotron-3-embed-1b via NVIDIA NIM, text-only, 2048-dim;
  overridable with EMBEDDING_MODEL / EMBEDDING_API_BASE)
- Quantization: PyTorch dynamic int8 on CPU — no CUDA required, ~4x memory reduction
- Index type: FAISS IndexHNSWFlat (approx NN, much faster search than flat for large corpora)
- Persistence: each KB saves a .faiss index + .pkl document map locally, then syncs to S3
- Embeddings are stored in the pickle so deletion never requires re-encoding
- S3 sync is best-effort: if AWS is not configured, local-only mode is used silently
- Version tracking: each index stores the model name that created it; on model
  change the index is automatically rebuilt from stored text content.
"""
import asyncio
import hashlib
import logging
import os
import pickle
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Coroutine, Dict, List, TypeVar

import httpx
import numpy as np
from asgiref.sync import sync_to_async
from django.conf import settings

from workflow_backend.background import spawn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global embedder singleton
# Model : nvidia/nemotron-3-embed-1b  (served via NVIDIA NIM, OpenAI-compatible)
# Dim   : 2048
#
# Repointed 2026-09-01: nv-embedqa-e5-v5 reached end of life on 2026-08-25 and
# NIM answers 410 for it, which took document ingestion and every KB search
# down with it. A model name is a perishable dependency — EMBEDDER_VERSION
# below is what turns a swap into an automatic re-index rather than a corpus
# of embeddings silently in the wrong space.
#
# Embeddings are produced by an external API rather than a local model — the
# 912MB box cannot hold sentence-transformers/torch in RAM. The same
# NVIDIA_API_KEY that powers chat is reused here. Override the model/endpoint
# with the EMBEDDING_MODEL / EMBEDDING_API_BASE env vars.
#
# On startup every KB whose stored version differs from EMBEDDER_VERSION is
# automatically re-indexed in the background (see initialize()).
# ---------------------------------------------------------------------------

_global_embedder = None
_embedder_lock = asyncio.Lock()
EMBEDDING_DIM = int(os.environ.get('EMBEDDING_DIM', '2048'))
EMBEDDING_MODEL = os.environ.get('EMBEDDING_MODEL', 'nvidia/nemotron-3-embed-1b')
EMBEDDING_API_BASE = os.environ.get(
    'EMBEDDING_API_BASE', 'https://integrate.api.nvidia.com/v1'
)

# Bump this whenever the model weights, tokenizer, or pooling strategy change
# in a way that makes old embeddings incompatible with new ones.
EMBEDDER_VERSION = f'{EMBEDDING_MODEL}:{EMBEDDING_DIM}'

# Bounded residency: a loaded KB stays in RAM only while it is being used.
# After this much idleness the sweeper drops it; the next caller reloads
# through initialize() (local disk first, S3 fallback) at cold-start cost.
KB_IDLE_EVICTION_SECONDS = float(os.environ.get('KB_IDLE_EVICTION_SECONDS', '300'))
# How often the sweeper looks for idle KBs. Nothing else about eviction is
# time-sensitive, so a coarse interval is fine — it bounds how long a dead KB
# overstays its TTL, not any latency.
KB_IDLE_SWEEP_SECONDS = float(os.environ.get('KB_IDLE_SWEEP_SECONDS', '60'))


class RemoteEmbedder:
    """
    API-backed embedder. Calls an OpenAI-compatible /embeddings endpoint
    (NVIDIA NIM's nv-embedqa-e5-v5, 1024-dim) and returns a list of
    L2-normalised numpy arrays (N, EMBEDDING_DIM). No local model or torch
    required — nothing to load into RAM.

    NVIDIA retrieval models need an `input_type` hint: use "passage" when
    indexing documents and "query" when embedding a search query.
    """

    # NVIDIA's embeddings endpoint caps how many inputs it accepts per call.
    _MAX_BATCH = 32

    def __init__(self, api_key: str, model: str, base_url: str, dim: int):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip('/')
        self._dim = dim
        self._client = httpx.Client(timeout=60.0)

    def _call(self, inputs: list[str], input_type: str) -> list:
        resp = self._client.post(
            f'{self._base_url}/embeddings',
            headers={'Authorization': f'Bearer {self._api_key}'},
            json={
                'model': self._model,
                'input': inputs,
                'input_type': input_type,
                'encoding_format': 'float',
                'truncate': 'END',  # never fail on long chunks
            },
        )
        # The one HTTP site every embedding passes through, so it is the one
        # place worth translating at. A raw HTTPStatusError escaping from here
        # reached `rag_search` as an unhandled 500 — the view catches
        # KnowledgeBaseUnavailable and answers 503 precisely so a broken
        # embedder is distinguishable from an empty corpus, and an untyped
        # error walked straight past that. A retired model is the case that
        # matters: it is not transient, and the message has to name the model
        # because only an operator editing EMBEDDING_MODEL can fix it.
        if resp.status_code >= 400:
            detail = resp.text[:300].replace('\n', ' ')
            if resp.status_code in (404, 410):
                raise KnowledgeBaseUnavailable(
                    f'The embedding model {self._model!r} is no longer served by '
                    f'{self._base_url} ({resp.status_code}). Set EMBEDDING_MODEL '
                    f'(and EMBEDDING_DIM) to a current model and re-index. '
                    f'Provider said: {detail}'
                )
            raise KnowledgeBaseUnavailable(
                f'The embedding endpoint returned {resp.status_code} for model '
                f'{self._model!r}: {detail}'
            )
        # Preserve request order — the API tags each row with its index.
        rows = sorted(resp.json()['data'], key=lambda d: d.get('index', 0))
        return [r['embedding'] for r in rows]

    def encode(self, texts: list[str], batch_size: int = 32,
               input_type: str = 'passage') -> list:
        """
        Encode *texts* into normalised numpy arrays (N, EMBEDDING_DIM).

        Requests are chunked to stay under the endpoint's per-call input cap.
        """
        # The API rejects empty strings; substitute a single space.
        safe = [t if (isinstance(t, str) and t.strip()) else ' ' for t in texts]
        batch = min(batch_size, self._MAX_BATCH)
        out: list = []
        for i in range(0, len(safe), batch):
            for emb in self._call(safe[i:i + batch], input_type):
                v = np.asarray(emb, dtype='float32')
                norm = float(np.linalg.norm(v))
                if norm > 0:
                    v = v / norm
                out.append(v)
        return out


def _load_embedder() -> RemoteEmbedder:
    api_key = getattr(settings, 'NVIDIA_API_KEY', '') or os.environ.get('NVIDIA_API_KEY', '')
    if not api_key:
        raise RuntimeError(
            'NVIDIA_API_KEY is not configured — required for the embeddings API.'
        )
    logger.info(f"[Embedder] Using API model {EMBEDDING_MODEL} @ {EMBEDDING_API_BASE}")
    return RemoteEmbedder(api_key, EMBEDDING_MODEL, EMBEDDING_API_BASE, EMBEDDING_DIM)


def _preload_embedder():
    """Called from InferenceConfig.ready() after all Django modules are loaded."""
    global _global_embedder
    if _global_embedder is not None:
        return
    try:
        _global_embedder = _load_embedder()
    except Exception as e:
        logger.warning(f"[Embedder] Preload failed (will retry on first use): {e}")


async def get_global_embedder() -> RemoteEmbedder:
    global _global_embedder
    if _global_embedder is not None:
        return _global_embedder
    async with _embedder_lock:
        if _global_embedder is not None:
            return _global_embedder
        _global_embedder = await asyncio.to_thread(_load_embedder)
        return _global_embedder


# ---------------------------------------------------------------------------
# S3 helpers (best-effort, silent if AWS not configured)
# ---------------------------------------------------------------------------

def _s3_configured() -> bool:
    return bool(
        getattr(settings, 'AWS_ACCESS_KEY_ID', '') and
        getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '')
    )


def _get_s3_client():
    import boto3
    return boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=getattr(settings, 'AWS_S3_REGION_NAME', 'us-east-1'),
        endpoint_url=getattr(settings, 'AWS_S3_ENDPOINT_URL', None) or None,
    )


def _upload_to_s3(local_path: Path, s3_key: str) -> bool:
    if not _s3_configured():
        return False
    try:
        client = _get_s3_client()
        client.upload_file(
            str(local_path),
            settings.AWS_STORAGE_BUCKET_NAME,
            s3_key,
            ExtraArgs={'ACL': 'private'},
        )
        logger.info(f"Uploaded {local_path.name} to s3://{settings.AWS_STORAGE_BUCKET_NAME}/{s3_key}")
        return True
    except Exception as e:
        logger.error(f"S3 upload failed for {s3_key}: {e}")
        return False


def _download_from_s3(s3_key: str, local_path: Path) -> bool:
    if not _s3_configured():
        return False
    try:
        client = _get_s3_client()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(settings.AWS_STORAGE_BUCKET_NAME, s3_key, str(local_path))
        logger.info(f"Downloaded s3://{settings.AWS_STORAGE_BUCKET_NAME}/{s3_key} → {local_path}")
        return True
    except Exception as e:
        logger.debug(f"S3 download skipped for {s3_key}: {e}")
        return False


def _delete_from_s3(s3_key: str) -> bool:
    if not _s3_configured():
        return False
    try:
        client = _get_s3_client()
        client.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=s3_key)
        return True
    except Exception as e:
        logger.error(f"S3 delete failed for {s3_key}: {e}")
        return False


# ---------------------------------------------------------------------------
# Search result
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    document_id: int
    chunk_id: str
    content: str
    score: float        # cosine similarity in [0, 1]
    metadata: dict
    is_image: bool = False


class KnowledgeBaseUnavailable(RuntimeError):
    """A KB could not be opened — no embedder, unreadable index, no such KB.

    Raised rather than degraded into an empty result set: retrieval that
    silently answers "nothing found" when the machinery is broken is the same
    failure the chat turn's preflight exists to prevent.
    """


# ---------------------------------------------------------------------------
# HNSWKnowledgeBase — one instance per KnowledgeBase DB row
# ---------------------------------------------------------------------------

class HNSWKnowledgeBase:
    """
    Persistent HNSW-based vector store for a single named KB.

    Index format: FAISS IndexHNSWFlat (L2, normalized vectors → cosine similarity).
    Persistence: .faiss file (index graph) + .pkl file (document/embedding map).
    Both files are saved locally and uploaded to S3 on every write.
    """

    HNSW_M = 32              # connections per node — higher = better recall, more memory
    HNSW_EF_CONSTRUCTION = 200
    HNSW_EF_SEARCH = 64
    # Score floor lives in workflow_backend.thresholds (SEARCH_MIN_SCORE) —
    # `_search_inner` reads it from there, so a constant here would be a
    # second number nothing consults.

    def __init__(self, kb_id: int, s3_key_prefix: str = ''):
        self.kb_id = kb_id
        self._s3_prefix = s3_key_prefix or f'indices/kb_{kb_id}'
        self._index = None
        # int_idx → {'doc_id': int, 'content': str, 'metadata': dict, 'embedding': np.ndarray, 'is_image': bool}
        self._documents: Dict[int, dict] = {}
        self._embedder = None
        self._initialized = False
        self._reindexing = False
        self._lock = asyncio.Lock()
        # The version string of the embedder that generated _documents
        self._stored_version: str | None = None
        # Idle-eviction state. `_state_lock` is a threading.Lock because the
        # sweeper runs on its own thread while operations run on whatever loop
        # (ASGI or inference-kb-loop) called them; it only ever guards these
        # three small fields, never index mutation.
        self._state_lock = threading.Lock()
        self._last_used = time.monotonic()
        self._active_ops = 0

    # ---- Idle-eviction accounting --------------------------------------------

    def _op_begin(self):
        """Mark the KB active and pin it against eviction for the op's duration."""
        with self._state_lock:
            self._last_used = time.monotonic()
            self._active_ops += 1

    def _op_end(self):
        with self._state_lock:
            self._active_ops -= 1

    def is_evictable(self) -> bool:
        """
        True when the KB has been idle past the TTL and nothing is running.

        The in-flight check is a correctness guard, not tidiness: evicting an
        instance mid-operation would leave the op mutating an orphaned copy
        while the next get() built a second live one — two copies, and the
        writes to the first silently lost on process exit.
        """
        with self._state_lock:
            idle_for = time.monotonic() - self._last_used
            return (
                idle_for >= KB_IDLE_EVICTION_SECONDS
                and self._active_ops == 0
                and not self._reindexing
            )

    # ---- Initialization / persistence ----------------------------------------

    @property
    def _local_index_path(self) -> Path:
        return settings.FAISS_INDEX_DIR / f'kb_{self.kb_id}.faiss'

    @property
    def _local_docs_path(self) -> Path:
        return settings.FAISS_INDEX_DIR / f'kb_{self.kb_id}_docs.pkl'

    @property
    def _s3_index_key(self) -> str:
        return f'{self._s3_prefix}.faiss'

    @property
    def _s3_docs_key(self) -> str:
        return f'{self._s3_prefix}_docs.pkl'

    def _create_fresh_index(self):
        import faiss
        index = faiss.IndexHNSWFlat(EMBEDDING_DIM, self.HNSW_M)
        index.hnsw.efConstruction = self.HNSW_EF_CONSTRUCTION
        index.hnsw.efSearch = self.HNSW_EF_SEARCH
        return index

    def _save_local(self):
        try:
            import faiss
            settings.FAISS_INDEX_DIR.mkdir(parents=True, exist_ok=True)
            faiss.write_index(self._index, str(self._local_index_path))
            # Persist documents AND version in a single bundle
            bundle = {
                '_version': EMBEDDER_VERSION,
                'documents': self._documents,
            }
            with open(self._local_docs_path, 'wb') as f:
                pickle.dump(bundle, f)
        except Exception as e:
            logger.error(f"[KB {self.kb_id}] Local save failed: {e}")

    def _load_local(self) -> bool:
        try:
            if not self._local_index_path.exists() or not self._local_docs_path.exists():
                return False
            import faiss
            self._index = faiss.read_index(str(self._local_index_path))
            self._index.hnsw.efSearch = self.HNSW_EF_SEARCH
            with open(self._local_docs_path, 'rb') as f:
                raw = pickle.load(f)

            # Supports both old format (plain dict) and new versioned bundle
            if isinstance(raw, dict) and '_version' in raw:
                self._stored_version = raw['_version']
                self._documents = raw['documents']
            else:
                # Legacy index — no version tag, treat as "unknown"
                self._stored_version = None
                self._documents = raw

            logger.info(
                f"[KB {self.kb_id}] Loaded from local disk "
                f"({self._index.ntotal} vectors, version={self._stored_version or 'legacy'})"
            )
            return True
        except Exception as e:
            logger.warning(f"[KB {self.kb_id}] Local load failed: {e}")
            return False

    def _sync_to_s3(self):
        """Upload both files to S3 (sync, run in thread for async callers)."""
        _upload_to_s3(self._local_index_path, self._s3_index_key)
        _upload_to_s3(self._local_docs_path, self._s3_docs_key)

    def _fetch_from_s3(self) -> bool:
        idx_ok = _download_from_s3(self._s3_index_key, self._local_index_path)
        docs_ok = _download_from_s3(self._s3_docs_key, self._local_docs_path)
        return idx_ok and docs_ok

    async def initialize(self):
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            try:
                self._embedder = await get_global_embedder()
                # 1. Try local disk
                if not self._load_local():
                    # 2. Try S3 fallback
                    fetched = await asyncio.to_thread(self._fetch_from_s3)
                    if fetched:
                        self._load_local()
                    else:
                        # 3. Fresh index
                        self._index = self._create_fresh_index()
                        self._documents = {}
                        self._stored_version = EMBEDDER_VERSION
                self._initialized = True

                # Check if the stored index was built with a different model
                if self._documents and self._stored_version != EMBEDDER_VERSION:
                    logger.warning(
                        f"[KB {self.kb_id}] Embedder version mismatch: "
                        f"stored={self._stored_version}, current={EMBEDDER_VERSION}. "
                        f"Scheduling background re-index."
                    )
                    # Fire-and-forget background re-index. Detached from the
                    # request context: the re-index long outlives the response
                    # that happened to trigger it.
                    spawn(self._background_reindex(), name=f"kb-reindex:{self.kb_id}")

            except Exception as e:
                # A KB that cannot be loaded must not answer "no results" — a
                # broken embedder and an empty corpus produced byte-identical
                # responses, so a missing NVIDIA_API_KEY looked to the user
                # exactly like a knowledge base with nothing in it. Same rule
                # the chat turn enforces with `llm.preflight()`.
                logger.error(f"[KB {self.kb_id}] Initialization failed: {e}")
                raise KnowledgeBaseUnavailable(
                    f'Knowledge base {self.kb_id} could not be opened: {e}'
                ) from e

    # ---- Embedding helpers ---------------------------------------------------

    async def _embed_text(self, text: str, input_type: str = 'passage') -> np.ndarray:
        results = await asyncio.to_thread(self._embedder.encode, [text], 32, input_type)
        return results[0]

    async def _embed_texts(self, texts: list[str], batch_size: int = 32) -> list[np.ndarray]:
        """Batch-embed a list of texts (runs encoding in a worker thread)."""
        return await asyncio.to_thread(self._embedder.encode, texts, batch_size, 'passage')

    # Public aliases for compatibility. These ensure the embedder is loaded
    # first: on a fresh worker `_embedder` is None until `initialize()` runs,
    # and callers like the skills/templates services embed directly without
    # having searched first, so without this guard the first embed crashes with
    # AttributeError — swallowed by a bare thread and seen only as an empty
    # inbox / an intermittent 500.
    async def embed_text(self, text: str) -> np.ndarray:
        self._op_begin()
        try:
            if not self._initialized:
                await self.initialize()
            return await self._embed_text(text)
        finally:
            self._op_end()

    async def embed_texts(self, texts: list[str], batch_size: int = 32) -> list[np.ndarray]:
        self._op_begin()
        try:
            if not self._initialized:
                await self.initialize()
            return await self._embed_texts(texts, batch_size)
        finally:
            self._op_end()

    async def embed_query(self, query: str) -> np.ndarray:
        """Embed a question once so several KBs can be searched with one vector."""
        self._op_begin()
        try:
            if not self._initialized:
                await self.initialize()
            return await self._embed_text(query, input_type='query')
        finally:
            self._op_end()

    # ---- Re-indexing ---------------------------------------------------------

    async def rebuild_index(self):
        """
        Re-embed every document stored in this KB using the current embedder
        and rebuild the FAISS HNSW index from scratch.

        This is safe to call while the KB is live — searches against the *old*
        index continue to work until the rebuild completes, at which point the
        new index is swapped in atomically under the write lock.
        """
        self._op_begin()
        try:
            return await self._rebuild_index_inner()
        finally:
            self._op_end()

    async def _rebuild_index_inner(self):
        if not self._documents:
            logger.info(f"[KB {self.kb_id}] Nothing to re-index (empty).")
            self._stored_version = EMBEDDER_VERSION
            await asyncio.to_thread(self._save_local)
            return

        total = len(self._documents)
        logger.info(f"[KB {self.kb_id}] Re-indexing {total} chunks with {EMBEDDER_VERSION}…")

        # Collect all content texts, preserving order by int_idx
        sorted_items = sorted(self._documents.items(), key=lambda kv: kv[0])
        texts = [item['content'] for _, item in sorted_items]

        # Batch-embed outside the lock
        new_embeddings = await self._embed_texts(texts)

        # Swap in the new index under the write lock
        async with self._lock:
            new_index = self._create_fresh_index()
            new_docs: Dict[int, dict] = {}
            for new_idx, ((_, item), emb) in enumerate(zip(sorted_items, new_embeddings)):
                new_index.add(np.array([emb], dtype='float32'))
                new_docs[new_idx] = {
                    **item,
                    'embedding': emb,
                }
            self._index = new_index
            self._documents = new_docs
            self._stored_version = EMBEDDER_VERSION

            await asyncio.to_thread(self._save_local)

        # S3 outside the lock: the local write is what makes the change
        # durable, the upload is a replica. Holding the write lock across a
        # network round-trip blocked every concurrent write to this KB.
        await asyncio.to_thread(self._sync_to_s3)

        logger.info(f"[KB {self.kb_id}] Re-index complete — {total} chunks, version={EMBEDDER_VERSION}")

    async def _background_reindex(self):
        """Fire-and-forget wrapper that catches all errors."""
        if self._reindexing:
            return
        self._reindexing = True
        try:
            await self.rebuild_index()
        except Exception as e:
            logger.error(f"[KB {self.kb_id}] Background re-index failed: {e}", exc_info=True)
        finally:
            self._reindexing = False

    # ---- Public write API ---------------------------------------------------

    async def add_document(
        self,
        doc_id: int,
        content: str,
        metadata: dict | None = None,
        chunk_size: int = None,
        chunk_overlap: int = None,
    ) -> List[str]:
        from workflow_backend.thresholds import CHUNK_SIZE, CHUNK_OVERLAP
        self._op_begin()
        try:
            if not self._initialized:
                await self.initialize()

            chunk_size = chunk_size or CHUNK_SIZE
            chunk_overlap = chunk_overlap or CHUNK_OVERLAP
            chunks = _chunk_text(content, chunk_size, chunk_overlap)
            chunk_ids = []

            # Embed outside the lock (CPU-bound, slow) — the lock only guards the
            # index-mutation phase so concurrent adds can't corrupt index/document map.
            #
            # One batched call, not one per chunk: the embedder is a remote API, so
            # a chunk-at-a-time loop paid a full network round-trip per chunk and a
            # long document is hundreds of chunks. `_embed_texts` batches internally
            # and was already here — it just was not being called.
            embeddings = await self._embed_texts(chunks) if chunks else []

            async with self._lock:
                for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                    int_idx = len(self._documents)
                    self._index.add(np.array([embedding], dtype='float32'))
                    self._documents[int_idx] = {
                        'doc_id': doc_id,
                        'content': chunk,
                        'metadata': metadata or {},
                        'embedding': embedding,
                        'is_image': False,
                    }
                    chunk_ids.append(f'doc_{doc_id}_chunk_{i}')

                await asyncio.to_thread(self._save_local)

            # Replica push happens after the lock is dropped (see rebuild_index).
            await asyncio.to_thread(self._sync_to_s3)
            logger.info(f"[KB {self.kb_id}] Added doc {doc_id}: {len(chunks)} chunks")
            return chunk_ids
        finally:
            self._op_end()

    async def delete_document(self, doc_id: int) -> bool:
        self._op_begin()
        try:
            return await self._delete_document_inner(doc_id)
        finally:
            self._op_end()

    async def _delete_document_inner(self, doc_id: int) -> bool:
        if not self._initialized:
            await self.initialize()

        async with self._lock:
            remaining = {
                k: v for k, v in self._documents.items() if v['doc_id'] != doc_id
            }
            if len(remaining) == len(self._documents):
                return False

            # Rebuild index from stored embeddings (no re-encoding needed)
            self._index = self._create_fresh_index()
            new_docs = {}
            for new_idx, item in enumerate(remaining.values()):
                self._index.add(np.array([item['embedding']], dtype='float32'))
                new_docs[new_idx] = item
            self._documents = new_docs

            await asyncio.to_thread(self._save_local)
            remaining_count = len(new_docs)

        # Replica push happens after the lock is dropped (see rebuild_index).
        await asyncio.to_thread(self._sync_to_s3)
        logger.info(f"[KB {self.kb_id}] Deleted doc {doc_id}, {remaining_count} chunks remain")
        return True

    async def has_document(self, doc_id: int) -> bool:
        return any(v['doc_id'] == doc_id for v in self._documents.values())

    # ---- Public read API ----------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = None,
        doc_id: int | None = None,
        query_embedding: np.ndarray | None = None,
    ) -> List[SearchResult]:
        from workflow_backend.thresholds import SEARCH_TOP_K, SEARCH_MIN_SCORE
        self._op_begin()
        try:
            return await self._search_inner(
                query, top_k, min_score, doc_id, query_embedding,
                SEARCH_TOP_K, SEARCH_MIN_SCORE,
            )
        finally:
            self._op_end()

    async def _search_inner(
        self,
        query: str,
        top_k: int,
        min_score: float,
        doc_id: int | None,
        query_embedding: np.ndarray | None,
        default_top_k: int,
        default_min_score: float,
    ) -> List[SearchResult]:
        if not self._initialized:
            await self.initialize()
        if not self._initialized or self._index is None or self._index.ntotal == 0:
            return []

        min_score = min_score if min_score is not None else default_min_score
        top_k = top_k or default_top_k

        # Callers searching several knowledge bases with one question embed it
        # once and hand the vector down. Every KB shares `_global_embedder`, so
        # the vector is portable between them; re-deriving it per KB was a
        # second network round-trip for a byte-identical result.
        query_emb = (
            query_embedding if query_embedding is not None
            else await self._embed_text(query, input_type='query')
        )
        search_k = min(top_k * 3, self._index.ntotal)
        distances, indices = self._index.search(
            np.array([query_emb], dtype='float32'), search_k
        )

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0:
                continue
            # Convert L2 distance → cosine similarity. For unit vectors
            # d² = 2 - 2·cos, so cos = 1 - d²/2 — and FAISS METRIC_L2 already
            # returns the *squared* distance, so `dist` is d², not d.
            #
            # This squared `dist` again (`1 - dist**2/2`), which understated
            # every score in the corpus and silently cost recall: a chunk whose
            # true cosine was 0.398 scored 0.276 and fell under the 0.3 floor in
            # `SEARCH_MIN_SCORE`, so a document that was indexed, present and
            # relevant came back as "no results". Verified against the stored
            # vector: 1 - 1.2030/2 = 0.39849913 and the true cosine is
            # 0.39849922, while the old formula gave 0.27639.
            cosine = float(1.0 - dist / 2.0)
            if cosine < min_score:
                continue

            item = self._documents.get(int(idx))
            if item is None:
                continue
            if doc_id is not None and item['doc_id'] != doc_id:
                continue

            results.append(SearchResult(
                document_id=item['doc_id'],
                chunk_id=f'chunk_{idx}',
                content=item['content'],
                score=cosine,
                metadata=item['metadata'],
                is_image=item.get('is_image', False),
            ))
            if len(results) >= top_k:
                break

        return results

    def destroy_local(self):
        """Remove local index files (e.g. when KB is deleted)."""
        for path in [self._local_index_path, self._local_docs_path]:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    @property
    def stored_version(self) -> str | None:
        """Embedder version the loaded index was built with (None if legacy).

        Public because the re-index sweep has to compare it against
        `EMBEDDER_VERSION` to stay idempotent; it was reaching into
        `_stored_version` from two different modules.
        """
        return self._stored_version

    @property
    def is_loaded(self) -> bool:
        """Whether this instance holds a real index right now.

        Stats writers check it: an evicted-but-not-reloaded instance reports
        `ntotal == 0`, which would zero a KB's vector_count on disk without
        anything having been deleted.
        """
        return self._initialized and self._index is not None

    @property
    def ntotal(self) -> int:
        return self._index.ntotal if self._index else 0

    @property
    def index_size_bytes(self) -> int:
        try:
            return self._local_index_path.stat().st_size + self._local_docs_path.stat().st_size
        except Exception:
            return 0

    def clear(self):
        if self._index:
            self._index.reset()
        self._documents.clear()


# ---------------------------------------------------------------------------
# KnowledgeBaseManager — process-level cache of loaded KB instances
# ---------------------------------------------------------------------------

class KnowledgeBaseManager:
    """Maps KB db-id → in-memory HNSWKnowledgeBase, lazy-loaded.

    Residency is bounded by TTL, not forever: a background sweeper drops
    instances that have been idle past KB_IDLE_EVICTION_SECONDS, so RSS
    tracks the working set of active users instead of every KB ever touched.
    """

    _instance: 'KnowledgeBaseManager | None' = None

    def __init__(self):
        self._kbs: Dict[int, HNSWKnowledgeBase] = {}
        # Guards the registry dict itself. Per-KB asyncio locks stay untouched —
        # this only makes get/evict/evict_idle safe across the several threads
        # that reach the manager (ASGI loop, inference-kb-loop, sweeper).
        self._registry_lock = threading.Lock()

    def get(self, kb_id: int, s3_key_prefix: str = '') -> HNSWKnowledgeBase:
        with self._registry_lock:
            kb = self._kbs.get(kb_id)
            if kb is None:
                kb = HNSWKnowledgeBase(kb_id, s3_key_prefix)
                self._kbs[kb_id] = kb
            return kb

    def evict(self, kb_id: int):
        with self._registry_lock:
            self._kbs.pop(kb_id, None)

    def evict_idle(self) -> list[int]:
        """Drop idle KBs from memory. Never touches local files or S3 —
        an evicted KB reloads through initialize() exactly like a cold start."""
        with self._registry_lock:
            victims = [kb_id for kb_id, kb in self._kbs.items() if kb.is_evictable()]
            for kb_id in victims:
                self._kbs.pop(kb_id, None)
        return victims


_sweeper_thread: threading.Thread | None = None
_sweeper_lock = threading.Lock()


def _idle_sweep_loop():
    while True:
        time.sleep(KB_IDLE_SWEEP_SECONDS)
        try:
            evicted = get_kb_manager().evict_idle()
            if evicted:
                logger.info(
                    f"[Embedder] Evicted {len(evicted)} idle KB(s) after "
                    f"{KB_IDLE_EVICTION_SECONDS:.0f}s: {evicted}"
                )
        except Exception:
            logger.exception("[Embedder] Idle-KB sweep failed")


def _ensure_sweeper():
    global _sweeper_thread
    with _sweeper_lock:
        if _sweeper_thread is None or not _sweeper_thread.is_alive():
            _sweeper_thread = threading.Thread(
                target=_idle_sweep_loop,
                name='kb-idle-sweeper',
                daemon=True,
            )
            _sweeper_thread.start()


_kb_manager: KnowledgeBaseManager | None = None
_kb_manager_lock = threading.Lock()


def get_kb_manager() -> KnowledgeBaseManager:
    global _kb_manager
    if _kb_manager is None:
        with _kb_manager_lock:
            if _kb_manager is None:
                _kb_manager = KnowledgeBaseManager()
        _ensure_sweeper()
    return _kb_manager


# ---------------------------------------------------------------------------
# High-level helpers used by tasks.py, views.py, chat/tools.py
# ---------------------------------------------------------------------------

async def get_or_create_default_kb(user) -> 'Any':
    """
    Get (or lazily create) the user's Default KB DB record.
    Returns the KnowledgeBase ORM instance.
    """
    from .models import KnowledgeBase

    def _db_op():
        kb, _ = KnowledgeBase.objects.get_or_create(
            user=user,
            is_default=True,
            defaults={'name': 'Default', 'description': 'Auto-created default knowledge base'},
        )
        return kb

    return await sync_to_async(_db_op)()


def get_hnsw_kb(kb_db_id: int, s3_key_prefix: str = '') -> HNSWKnowledgeBase:
    """Get the in-memory HNSW instance for a KB DB id."""
    return get_kb_manager().get(kb_db_id, s3_key_prefix)


async def get_kb_for_user(user_id: int, kb_id: int | None = None) -> 'tuple[Any, HNSWKnowledgeBase]':
    """
    Return (KBModel, HNSWKnowledgeBase) for the given user.
    If kb_id is None, uses the user's default KB.
    """
    from .models import KnowledgeBase
    from django.contrib.auth import get_user_model

    User = get_user_model()

    def _get():
        user = User.objects.get(id=user_id)
        if kb_id is not None:
            return KnowledgeBase.objects.get(id=kb_id, user=user), user
        kb, _ = KnowledgeBase.objects.get_or_create(
            user=user,
            is_default=True,
            defaults={'name': 'Default', 'description': 'Auto-created default knowledge base'},
        )
        return kb, user

    kb_model, _ = await sync_to_async(_get)()
    hnsw = get_hnsw_kb(kb_model.id, kb_model.s3_index_key or f'indices/kb_{kb_model.id}')
    await hnsw.initialize()
    return kb_model, hnsw


def sync_kb_stats(kb_model_id: int, hnsw: 'HNSWKnowledgeBase | None' = None) -> None:
    """Recount a KB's document / vector / size stats from the live state.

    Sync, because every caller (ingest, delete) is already in sync context.
    `hnsw` is optional: pass it when a vector index actually backs this KB,
    omit it for fulltext / raw KBs whose vector columns must stay untouched
    rather than be zeroed.

    Called from *both* ends of a document's life. Only ingest used to update
    these, so deleting a document left the count it was counted in behind —
    and `chat/tools/knowledge.py` reports that count to the agent, which meant
    the model was told an emptied KB still held documents.
    """
    from .models import KnowledgeBase, Document

    updates = {
        'doc_count': Document.objects.filter(knowledge_base_id=kb_model_id).count(),
    }
    if hnsw is not None:
        updates['vector_count'] = hnsw.ntotal
        updates['index_size_bytes'] = hnsw.index_size_bytes
    KnowledgeBase.objects.filter(id=kb_model_id).update(**updates)


async def update_kb_stats(kb_model_id: int, hnsw: HNSWKnowledgeBase):
    """Async wrapper over `sync_kb_stats` for callers already on a loop."""
    await sync_to_async(sync_kb_stats)(kb_model_id, hnsw)


# ---------------------------------------------------------------------------
# Per-user / shared KB accessors
#
# Reserved negative ids: -1 platform, -2 skills, and session KBs below
# -10_000_000. Nothing may derive a KB id from a user id — the arithmetic
# fallback that used to live here (`get_hnsw_kb(-user_id)`) mapped user 1 onto
# the platform KB and user 2 onto the skills index, so a user's own RAG query
# read someone else's corpus. A KB that cannot be resolved is an error, not a
# different KB.
# ---------------------------------------------------------------------------

async def aget_user_knowledge_base(user_id: int) -> HNSWKnowledgeBase:
    """Async accessor for the user's default KB, creating it if absent."""
    from .models import KnowledgeBase

    def _get():
        kb, _ = KnowledgeBase.objects.get_or_create(
            user_id=user_id,
            is_default=True,
            defaults={'name': 'Default', 'description': 'Auto-created default knowledge base'},
        )
        return kb.id, kb.s3_index_key

    kb_id, s3_key = await sync_to_async(_get)()
    return get_hnsw_kb(kb_id, s3_key or f'indices/kb_{kb_id}')


def get_platform_knowledge_base() -> HNSWKnowledgeBase:
    """Returns the shared platform KB (id=-1 by convention)."""
    return get_hnsw_kb(-1, 'indices/platform')


SKILLS_KB_ID = -2


def get_skills_knowledge_base() -> HNSWKnowledgeBase:
    """Shared FAISS index backing skill search (id=-2 by convention).

    One document per skill (chunked internally by `add_document`), with
    `skill_id` / `user_id` / `category` in the chunk metadata. The `Skill`
    table stays the source of truth; this is a retrieval projection of it.
    """
    return get_hnsw_kb(SKILLS_KB_ID, 'indices/skills')


# ---------------------------------------------------------------------------
# Sync bridge for the KB event loop
# ---------------------------------------------------------------------------
# `HNSWKnowledgeBase` methods are async and guard index mutation with an
# `asyncio.Lock`. The lock binds to the first loop that ever touches it, so
# running `asyncio.run` in an ad-hoc thread raises "bound to a different event
# loop" the moment two threads race on a cold KB. Everything that drives the KB
# from sync code (skills embedding, seed commands) must land on one dedicated
# loop; these helpers own it.

_T = TypeVar('_T')

_kb_loop: 'asyncio.AbstractEventLoop | None' = None
_kb_loop_lock = threading.Lock()


def _get_kb_loop() -> asyncio.AbstractEventLoop:
    global _kb_loop
    with _kb_loop_lock:
        if _kb_loop is None or _kb_loop.is_closed():
            _kb_loop = asyncio.new_event_loop()
            threading.Thread(
                target=_kb_loop.run_forever,
                name='inference-kb-loop',
                daemon=True,
            ).start()
        return _kb_loop


def submit_kb_async(coro: Coroutine[Any, Any, _T], *, name: str | None = None) -> Future[_T]:
    """Schedule a KB coroutine on the shared loop; returns its Future.

    Fire-and-forget callers attach `add_done_callback` to observe exceptions;
    blocking callers use `run_kb_async`.
    """
    return asyncio.run_coroutine_threadsafe(coro, _get_kb_loop())


def run_kb_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run a KB coroutine on the shared loop and block for the result.

    Safe from sync request threads: every KB call lands on the one loop, so
    the instance lock is never contended across loops.
    """
    return submit_kb_async(coro).result()


def get_session_knowledge_base(session_id: str) -> HNSWKnowledgeBase:
    """Ephemeral per-session KB at a negative synthetic id derived from the id.

    The digest is `blake2b`, not `hash()`: PYTHONHASHSEED is randomised per
    process, so the built-in gave a session a different KB id after every
    restart and a *different* id per ASGI worker — its index files were
    written once and never found again.
    """
    digest = hashlib.blake2b(session_id.encode('utf-8'), digest_size=8).digest()
    synthetic_id = -(int.from_bytes(digest, 'big') % 10_000_000 + 10_000_000)
    return get_hnsw_kb(synthetic_id, '')


def clear_session_kb(session_id: str) -> None:
    """Drop a session's ephemeral KB and its on-disk index."""
    hnsw = get_session_knowledge_base(session_id)
    hnsw.clear()
    hnsw.destroy_local()


async def get_rag_pipeline(user_id: int | None = None) -> 'RAGPipeline':
    """Pipeline over the user's default KB (or the platform KB when anonymous).

    Async because resolving the KB is a DB read: the sync version was being
    awaited from an async view, where every ORM call raises
    `SynchronousOnlyOperation` — swallowed, so the endpoint answered from a
    fallback KB on literally every request.
    """
    kb = (
        await aget_user_knowledge_base(user_id) if user_id
        else get_platform_knowledge_base()
    )
    return RAGPipeline(kb)


# ---------------------------------------------------------------------------
# RAGPipeline (kept for backward compat with rag_query view)
# ---------------------------------------------------------------------------

class RAGPipeline:
    def __init__(self, kb: HNSWKnowledgeBase):
        self.kb = kb

    async def query(self, question: str, user_id: int, llm_type: str = 'openai',
                    top_k: int = 5, credential_id=None, context=None) -> Dict:
        results = await self.kb.search(question, top_k=top_k)
        if not results:
            return {'answer': 'No relevant information found.', 'sources': [], 'no_context': True}

        context_text = '\n\n---\n\n'.join(
            f'Source {i+1} (score: {r.score:.2f}):\n{r.content}' for i, r in enumerate(results)
        )
        prompt = (
            f'Based on the following context, answer the user\'s question.\n'
            f'Context:\n{context_text}\n\nQuestion: {question}\n\nAnswer:'
        )

        from llm.handlers.registry import get_registry
        registry = get_registry()
        if not registry.has_handler(llm_type):
            return {'answer': f"LLM type '{llm_type}' not available", 'sources': [], 'error': True}

        handler = registry.get_handler(llm_type)
        if context is None:
            from llm.context import ExecutionContext
            context = ExecutionContext(user_id=user_id)

        config = {
            'prompt': prompt,
            'credential': credential_id,
            'temperature': 0.3,
        }
        # Omit rather than guess a model id for providers other than OpenAI;
        # the handler falls back to its own configured default.
        if llm_type == 'openai':
            config['model'] = 'gpt-4o-mini'
        try:
            result = await handler.execute({}, config, context)
            if result.success:
                data = result.data
                return {
                    'answer': data.get('content', ''),
                    'sources': [{'document_id': r.document_id, 'score': r.score} for r in results],
                }
            return {'answer': 'Failed to generate response', 'error': result.error, 'sources': []}
        except Exception as e:
            logger.exception(f'RAG query failed: {e}')
            return {'answer': f'Error: {e}', 'sources': [], 'error': True}


# ---------------------------------------------------------------------------
# Text chunking utility
# ---------------------------------------------------------------------------

def _chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    # Clamp overlap so the loop always advances.
    if overlap >= chunk_size:
        overlap = max(0, chunk_size // 2)
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if end < len(text):
            for sep in ['\n\n', '\n', '. ', ', ', ' ']:
                last_sep = chunk.rfind(sep)
                if last_sep > chunk_size // 2:
                    chunk = chunk[:last_sep + len(sep)]
                    end = start + len(chunk)
                    break
        chunks.append(chunk.strip())
        next_start = end - overlap
        # Guarantee progress even in pathological cases (avoid infinite loop).
        if next_start <= start:
            next_start = start + 1
        start = next_start
    return [c for c in chunks if c]
