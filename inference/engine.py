"""
Inference Engine — Persistent HNSW Knowledge Base

Key design decisions:
- Embedding model: Qwen/Qwen3-Embedding-0.6B (0.6B params, text-only, 1024-dim)
- Quantization: PyTorch dynamic int8 on CPU — no CUDA required, ~4x memory reduction
- Index type: FAISS IndexHNSWFlat (approx NN, much faster search than flat for large corpora)
- Persistence: each KB saves a .faiss index + .pkl document map locally, then syncs to S3
- Embeddings are stored in the pickle so deletion never requires re-encoding
- S3 sync is best-effort: if AWS is not configured, local-only mode is used silently
- Version tracking: each index stores the model name that created it; on model
  change the index is automatically rebuilt from stored text content.
"""
import asyncio
import logging
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from asgiref.sync import sync_to_async
from django.conf import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global embedder singleton
# Model : Qwen/Qwen3-Embedding-0.6B  (0.6B params, text-only)
# Quant : PyTorch dynamic int8 on CPU (torch.quantization.quantize_dynamic)
# Dim   : 1024
#
# To switch models, update EMBEDDING_MODEL and EMBEDDING_DIM below.
# On the next startup every KB whose stored version differs will be
# automatically re-indexed in the background (see _maybe_reindex).
# ---------------------------------------------------------------------------

_global_embedder = None
_embedder_lock = asyncio.Lock()
EMBEDDING_DIM = 1024
EMBEDDING_MODEL = 'Qwen/Qwen3-Embedding-0.6B'

# Bump this whenever the model weights, tokenizer, or pooling strategy change
# in a way that makes old embeddings incompatible with new ones.
EMBEDDER_VERSION = f'{EMBEDDING_MODEL}:{EMBEDDING_DIM}'


class QwenEmbedder:
    """
    Wraps Qwen3-Embedding-0.6B with PyTorch dynamic int8 quantization on CPU.
    Exposes encode(texts) returning list of normalised numpy arrays (N, 1024).
    """

    def __init__(self, model, tokenizer):
        self._model = model
        self._tokenizer = tokenizer

    def _last_token_pool(self, last_hidden_state, attention_mask):
        import torch
        seq_len = attention_mask.sum(dim=1) - 1
        batch_idx = torch.arange(last_hidden_state.size(0))
        return last_hidden_state[batch_idx, seq_len]

    def _normalise(self, vec: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def encode(self, texts: list[str], batch_size: int = 32) -> list:
        """
        Encode *texts* into normalised numpy arrays (N, EMBEDDING_DIM).

        For large lists the input is processed in batches of *batch_size* to
        keep peak memory bounded while still being faster than one-at-a-time.
        """
        import torch
        all_vecs: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            inputs = self._tokenizer(
                batch,
                return_tensors='pt',
                padding=True,
                truncation=True,
                max_length=512,
            )
            with torch.no_grad():
                out = self._model(**inputs)
                pooled = self._last_token_pool(out.last_hidden_state, inputs['attention_mask'])
            arr = pooled.float().numpy()  # (B, dim)
            all_vecs.extend(self._normalise(row) for row in arr)
        return all_vecs


def _load_qwen_embedder() -> QwenEmbedder:
    import torch
    from transformers import AutoTokenizer, AutoModel

    logger.info(f"[Embedder] Loading {EMBEDDING_MODEL} | device=cpu | quant=int8")

    cache_dir = Path(settings.BASE_DIR) / "static" / "model_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    tokenizer = AutoTokenizer.from_pretrained(
        EMBEDDING_MODEL, trust_remote_code=True, cache_dir=cache_dir
    )
    model = AutoModel.from_pretrained(
        EMBEDDING_MODEL,
        dtype=torch.float32,
        trust_remote_code=True,
        cache_dir=cache_dir,
    )
    model.eval()

    # Dynamic int8 quantization — all Linear layers quantized at inference time, CPU only
    model = torch.quantization.quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)

    logger.info(f"[Embedder] {EMBEDDING_MODEL} ready (int8, cpu).")
    return QwenEmbedder(model, tokenizer)


def _preload_embedder():
    """Called from InferenceConfig.ready() after all Django modules are loaded."""
    global _global_embedder
    if _global_embedder is not None:
        return
    try:
        _global_embedder = _load_qwen_embedder()
    except Exception as e:
        logger.warning(f"[Embedder] Preload failed (will retry on first use): {e}")


async def get_global_embedder() -> QwenEmbedder:
    global _global_embedder
    if _global_embedder is not None:
        return _global_embedder
    async with _embedder_lock:
        if _global_embedder is not None:
            return _global_embedder
        _global_embedder = await asyncio.to_thread(_load_qwen_embedder)
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
    MIN_SCORE = 0.25          # cosine similarity threshold (lower = more permissive)

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
                    # Fire-and-forget background re-index
                    asyncio.ensure_future(self._background_reindex())

            except Exception as e:
                logger.error(f"[KB {self.kb_id}] Initialization failed: {e}")

    # ---- Embedding helpers ---------------------------------------------------

    async def _embed_text(self, text: str) -> np.ndarray:
        results = await asyncio.to_thread(self._embedder.encode, [text])
        return results[0]

    async def _embed_texts(self, texts: list[str], batch_size: int = 32) -> list[np.ndarray]:
        """Batch-embed a list of texts (runs encoding in a worker thread)."""
        return await asyncio.to_thread(self._embedder.encode, texts, batch_size)

    # ---- Re-indexing ---------------------------------------------------------

    async def rebuild_index(self):
        """
        Re-embed every document stored in this KB using the current embedder
        and rebuild the FAISS HNSW index from scratch.

        This is safe to call while the KB is live — searches against the *old*
        index continue to work until the rebuild completes, at which point the
        new index is swapped in atomically under the write lock.
        """
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
        if not self._initialized:
            await self.initialize()

        chunk_size = chunk_size or CHUNK_SIZE
        chunk_overlap = chunk_overlap or CHUNK_OVERLAP
        chunks = _chunk_text(content, chunk_size, chunk_overlap)
        chunk_ids = []

        # Embed outside the lock (CPU-bound, slow) — the lock only guards the
        # index-mutation phase so concurrent adds can't corrupt index/document map.
        embeddings = []
        for chunk in chunks:
            embeddings.append(await self._embed_text(chunk))

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
            await asyncio.to_thread(self._sync_to_s3)
        logger.info(f"[KB {self.kb_id}] Added doc {doc_id}: {len(chunks)} chunks")
        return chunk_ids

    async def delete_document(self, doc_id: int) -> bool:
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
            await asyncio.to_thread(self._sync_to_s3)
            logger.info(f"[KB {self.kb_id}] Deleted doc {doc_id}, {len(new_docs)} chunks remain")
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
    ) -> List[SearchResult]:
        from workflow_backend.thresholds import SEARCH_TOP_K, SEARCH_MIN_SCORE
        if not self._initialized:
            await self.initialize()
        if not self._initialized or self._index is None or self._index.ntotal == 0:
            return []

        min_score = min_score if min_score is not None else SEARCH_MIN_SCORE
        top_k = top_k or SEARCH_TOP_K

        query_emb = await self._embed_text(query)
        search_k = min(top_k * 3, self._index.ntotal)
        distances, indices = self._index.search(
            np.array([query_emb], dtype='float32'), search_k
        )

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0:
                continue
            # Convert L2 distance → cosine similarity (for unit vectors: cos = 1 - d²/2)
            cosine = float(1.0 - (dist ** 2) / 2.0)
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
    """Maps KB db-id → in-memory HNSWKnowledgeBase, lazy-loaded."""

    _instance: 'KnowledgeBaseManager | None' = None

    def __init__(self):
        self._kbs: Dict[int, HNSWKnowledgeBase] = {}

    def get(self, kb_id: int, s3_key_prefix: str = '') -> HNSWKnowledgeBase:
        if kb_id not in self._kbs:
            self._kbs[kb_id] = HNSWKnowledgeBase(kb_id, s3_key_prefix)
        return self._kbs[kb_id]

    def evict(self, kb_id: int):
        self._kbs.pop(kb_id, None)


_kb_manager: KnowledgeBaseManager | None = None


def get_kb_manager() -> KnowledgeBaseManager:
    global _kb_manager
    if _kb_manager is None:
        _kb_manager = KnowledgeBaseManager()
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


async def update_kb_stats(kb_model_id: int, hnsw: HNSWKnowledgeBase):
    """Sync doc_count / vector_count / index_size_bytes back to the DB."""
    from .models import KnowledgeBase, Document

    def _update():
        doc_count = Document.objects.filter(knowledge_base_id=kb_model_id).count()
        KnowledgeBase.objects.filter(id=kb_model_id).update(
            doc_count=doc_count,
            vector_count=hnsw.ntotal,
            index_size_bytes=hnsw.index_size_bytes,
        )

    await sync_to_async(_update)()


# ---------------------------------------------------------------------------
# Backward-compat shims (referenced by old chat/tools.py paths)
# These search the user's default KB so existing callers keep working.
# ---------------------------------------------------------------------------

def get_user_knowledge_base(user_id: int) -> HNSWKnowledgeBase:
    """Sync shim — returns the in-memory HNSW KB for the user's default KB.
    The caller must still call .initialize() before use."""
    from .models import KnowledgeBase
    try:
        kb = KnowledgeBase.objects.filter(user_id=user_id, is_default=True).first()
        if kb:
            return get_hnsw_kb(kb.id)
    except Exception:
        pass
    # Fallback: create a transient HNSW KB at a predictable id
    return get_hnsw_kb(-(user_id))


def get_platform_knowledge_base() -> HNSWKnowledgeBase:
    """Returns the shared platform KB (id=-1 by convention)."""
    return get_hnsw_kb(-1, 'indices/platform')


def get_session_knowledge_base(session_id: str) -> HNSWKnowledgeBase:
    """Ephemeral per-session KB stored at a negative synthetic id derived from session hash."""
    synthetic_id = -(abs(hash(session_id)) % 10_000_000 + 10_000_000)
    return get_hnsw_kb(synthetic_id, '')


def get_session_kb_manager():
    """Compat shim — session KBs are now just HNSWKnowledgeBase instances in the manager."""
    class _Compat:
        def clear_session_kb(self, session_id: str):
            hnsw = get_session_knowledge_base(session_id)
            hnsw.clear()
            hnsw.destroy_local()
    return _Compat()


def get_rag_pipeline(user_id: int | None = None) -> 'RAGPipeline':
    kb = get_user_knowledge_base(user_id) if user_id else get_platform_knowledge_base()
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

        from nodes.handlers.registry import get_registry
        registry = get_registry()
        if not registry.has_handler(llm_type):
            return {'answer': f"LLM type '{llm_type}' not available", 'sources': [], 'error': True}

        handler = registry.get_handler(llm_type)
        if context is None:
            from compiler.schemas import ExecutionContext
            from uuid import uuid4
            context = ExecutionContext(execution_id=uuid4(), user_id=user_id, workflow_id=0)

        config = {
            'prompt': prompt,
            'credential': credential_id,
            'model': 'gpt-4o-mini' if llm_type == 'openai' else 'gemini-1.5-flash',
            'temperature': 0.3,
        }
        try:
            result = await handler.execute({}, config, context)
            if result.success:
                # NodeExecutionResult now exposes items — use get_data() to pull the first json payload.
                data = result.get_data() if hasattr(result, 'get_data') else (result.data or {})
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
