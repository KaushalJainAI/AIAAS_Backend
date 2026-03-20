"""
Inference Engine - RAG Query Pipeline and Knowledge Base

Implements:
- Knowledge base with FAISS/Chroma vector storage
- Document chunking and embedding
- RAG query pipeline for context-aware LLM responses
"""
import logging
import asyncio
import numpy as np
from typing import Any, List, Dict, Optional
from dataclasses import dataclass
from asgiref.sync import sync_to_async
from workflow_backend.thresholds import CHUNK_SIZE, CHUNK_OVERLAP, SEARCH_TOP_K, SEARCH_MIN_SCORE

logger = logging.getLogger(__name__)

# Global embedder variables for singleton pattern
_global_embedder = None
_embedder_lock = asyncio.Lock()


def _preload_embedder():
    """Pre-load the SentenceTransformer model in a background thread on server start.
    This eliminates cold-start latency on the first RAG search."""
    global _global_embedder
    if _global_embedder is not None:
        return
    try:
        from sentence_transformers import SentenceTransformer
        logger.info("[Background] Pre-loading SentenceTransformer model: all-MiniLM-L6-v2 on CPU...")
        _global_embedder = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')
        logger.info("[Background] SentenceTransformer model loaded and ready.")
    except Exception as e:
        logger.warning(f"[Background] Embedder preload failed (will retry on first use): {e}")


# Fire-and-forget: start warming up the embedder as soon as this module loads
import threading
threading.Thread(target=_preload_embedder, daemon=True).start()


async def get_global_embedder():
    """Get the global embedder. If already pre-loaded, returns instantly."""
    global _global_embedder
    
    if _global_embedder is not None:
        return _global_embedder
        
    async with _embedder_lock:
        if _global_embedder is not None:
            return _global_embedder
            
        def load_model():
            logger.info("Loading SentenceTransformer model: all-MiniLM-L6-v2 on CPU (fallback)")
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer('all-MiniLM-L6-v2', device='cpu')
            
        _global_embedder = await asyncio.to_thread(load_model)
        return _global_embedder


@dataclass
class SearchResult:
    """Result from a knowledge base search."""
    document_id: int
    chunk_id: str
    content: str
    score: float
    metadata: dict


class KnowledgeBase:
    """
    Vector-based knowledge base for RAG.
    
    Uses FAISS for local vector storage with sentence-transformer embeddings.
    
    Usage:
        kb = KnowledgeBase()
        await kb.initialize()
        await kb.add_document(doc_id=1, content="...", metadata={})
        results = await kb.search("query", top_k=5)
    """
    
    def __init__(self):
        self._index = None
        self._documents = {}  # chunk_id -> (doc_id, content, metadata)
        self._embedder = None
        self._initialized = False
        self._lock = asyncio.Lock()
    
    async def initialize(self):
        """Initialize the vector store and embedder."""
        if self._initialized:
            return
        
        async with self._lock:
            if self._initialized:
                return
            
            try:
                import faiss
                
                # Fetch the global embedder instance instead of creating a new one
                self._embedder = await get_global_embedder()
                
                # Create FAISS index (384 dimensions for MiniLM)
                self._index = faiss.IndexFlatIP(384)  # Inner product for cosine similarity
                self._initialized = True
                
                logger.info("Knowledge base initialized with FAISS and MiniLM embedder")
                
            except ImportError as e:
                logger.warning(f"FAISS or sentence-transformers not installed: {e}")
                self._initialized = False

    async def has_document(self, doc_id: int) -> bool:
        """Check if document already exists in the knowledge base."""
        # Simple linear scan. For production, maintain a separate set of doc_ids.
        for _, (existing_id, _, _) in self._documents.items():
            if existing_id == doc_id:
                return True
        return False

    async def add_document(
        self,
        doc_id: int,
        content: str,
        metadata: dict | None = None,
        chunk_size: int = CHUNK_SIZE,
        chunk_overlap: int = CHUNK_OVERLAP
    ) -> List[str]:
        """
        Add a document to the knowledge base.
        """
        if not self._initialized:
            await self.initialize()
            if not self._initialized:
                return []
        
        # Chunk the document
        chunks = self._chunk_text(content, chunk_size, chunk_overlap)
        chunk_ids = []
        
        for i, chunk in enumerate(chunks):
            chunk_id = f"doc_{doc_id}_chunk_{i}"
            
            # Generate embedding using to_thread
            embeddings = await asyncio.to_thread(self._embedder.encode, [chunk])
            embedding = embeddings[0]
            embedding = embedding / np.linalg.norm(embedding)  # Normalize
            
            # Add to index
            self._index.add(np.array([embedding], dtype='float32'))
            
            # Store document data
            self._documents[len(self._documents)] = (doc_id, chunk, metadata or {})
            chunk_ids.append(chunk_id)
        
        logger.info(f"Added document {doc_id} with {len(chunks)} chunks")
        return chunk_ids
    
    async def search(
        self,
        query: str,
        top_k: int = SEARCH_TOP_K,
        min_score: float = SEARCH_MIN_SCORE,
        doc_id: int | None = None
    ) -> List[SearchResult]:
        """
        Search the knowledge base.
        """
        if not self._initialized:
            await self.initialize()
            
        if not self._initialized or self._index.ntotal == 0:
            return []
        
        # Generate query embedding using to_thread
        query_embeddings = await asyncio.to_thread(self._embedder.encode, [query])
        query_embedding = query_embeddings[0]
        query_embedding = query_embedding / np.linalg.norm(query_embedding)
        
        # Search
        scores, indices = self._index.search(
            np.array([query_embedding], dtype='float32'),
            min(top_k, self._index.ntotal)
        )
        
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or score < min_score:
                continue
            
            existing_doc_id, content, metadata = self._documents.get(idx, (None, "", {}))
            
            # Apply doc_id filter if provided
            if doc_id is not None and existing_doc_id != doc_id:
                continue

            if existing_doc_id is not None:
                results.append(SearchResult(
                    document_id=existing_doc_id,
                    chunk_id=f"chunk_{idx}",
                    content=content,
                    score=float(score),
                    metadata=metadata
                ))
        
        return results
    
    async def embed_text(self, text: str) -> Any:
        """
        Generate embedding for a text string.
        """
        if not self._initialized:
            await self.initialize()
            if not self._initialized:
                return None
        
        # Use to_thread for CPU-bound encoding
        embeddings = await asyncio.to_thread(self._embedder.encode, [text])
        return embeddings[0]
    
    def _chunk_text(
        self,
        text: str,
        chunk_size: int,
        overlap: int
    ) -> List[str]:
        """Split text into overlapping chunks."""
        chunks = []
        start = 0
        
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]
            
            # Try to break at sentence or paragraph
            if end < len(text):
                for sep in ['\n\n', '\n', '. ', ', ', ' ']:
                    last_sep = chunk.rfind(sep)
                    if last_sep > chunk_size // 2:
                        chunk = chunk[:last_sep + len(sep)]
                        end = start + len(chunk)
                        break
            
            chunks.append(chunk.strip())
            start = end - overlap
        
        return [c for c in chunks if c]
    
    async def delete_document(self, doc_id: int) -> bool:
        """
        Remove a document from the knowledge base.
        Since FAISS IndexFlatIP doesn't support easy deletion by ID,
        we rebuild the index from the remaining chunks.
        """
        if not self._initialized:
            await self.initialize()
        
        async with self._lock:
            # 1. Identify chunks to keep
            remaining_docs = {}
            temp_chunks = []
            
            found = False
            for idx, (existing_id, content, metadata) in self._documents.items():
                if existing_id == doc_id:
                    found = True
                    continue
                remaining_docs[len(temp_chunks)] = (existing_id, content, metadata)
                temp_chunks.append(content)
            
            if not found:
                return False
                
            # 2. Re-embed and rebuild index if there are chunks left
            self._index.reset()
            self._documents = remaining_docs
            
            if temp_chunks:
                # Re-embedding is expensive, but necessary if we don't store 
                # the original embeddings. For now, we re-embed.
                embeddings = await asyncio.to_thread(self._embedder.encode, temp_chunks)
                
                # Normalize and add to index
                normalized_embeddings = []
                for emb in embeddings:
                    normalized_embeddings.append(emb / np.linalg.norm(emb))
                
                self._index.add(np.array(normalized_embeddings, dtype='float32'))
            
            logger.info(f"Deleted document {doc_id} and rebuilt index with {len(temp_chunks)} chunks")
            return True

    def clear(self):
        """Clear all documents from the knowledge base."""
        if self._index:
            self._index.reset()
        self._documents.clear()
        logger.info("Knowledge base cleared")


class RAGPipeline:
    """
    Retrieval-Augmented Generation pipeline.
    """
    
    def __init__(self, knowledge_base: KnowledgeBase):
        self.kb = knowledge_base
    
    async def query(
        self,
        question: str,
        user_id: int,
        llm_type: str = "openai",
        top_k: int = SEARCH_TOP_K,
        credential_id: str | None = None,
        context: Any = None
    ) -> Dict:
        """
        Run a RAG query.
        """
        # Search knowledge base(s)
        results = await self.kb.search(question, top_k=top_k)
        
        # In Hybrid RAG, we also check the session KB if a session_id is given
        session_id = None
        if isinstance(context, dict):
            session_id = context.get('session_id')
        elif hasattr(context, 'session_id'):
            session_id = context.session_id
            
        if session_id:
            session_kb = get_session_knowledge_base(str(session_id))
            if session_kb._initialized and session_kb._index and session_kb._index.ntotal > 0:
                session_results = await session_kb.search(question, top_k=top_k)
                # Merge and sort by score
                results.extend(session_results)
                results.sort(key=lambda x: x.score, reverse=True)
                # Take top_k overall
                results = results[:top_k]
                
        if not results:
            return {
                "answer": "I couldn't find relevant information in the knowledge base.",
                "sources": [],
                "no_context": True
            }
        
        # Build context from search results
        context_text = "\n\n---\n\n".join([
            f"Source {i+1} (score: {r.score:.2f}):\n{r.content}"
            for i, r in enumerate(results)
        ])
        
        # Build prompt
        prompt = f"""Based on the following context, answer the user's question.
    If the answer cannot be found in the context, say so.

    Context:
    {context_text}

    Question: {question}

    Answer:"""
        
        # Get LLM response
        from nodes.handlers.registry import get_registry
        registry = get_registry()
        
        if not registry.has_handler(llm_type):
            return {
                "answer": f"LLM type '{llm_type}' not available",
                "sources": [{"id": r.document_id, "score": r.score} for r in results],
                "error": True
            }
        
        handler = registry.get_handler(llm_type)
        
        # Build minimal execution context if not provided
        if context is None:
            from compiler.schemas import ExecutionContext
            from uuid import uuid4
            context = ExecutionContext(
                execution_id=uuid4(),
                user_id=user_id,
                workflow_id=0
            )
        
        # Execute LLM node
        config = {
            "prompt": prompt,
            "credential": credential_id,
            "model": "gpt-4o-mini" if llm_type == "openai" else "gemini-1.5-flash",
            "temperature": 0.3,
        }
        
        try:
            result = await handler.execute({}, config, context)
            
            if result.success:
                return {
                    "answer": result.data.get("content", ""),
                    "sources": [
                        {"document_id": r.document_id, "score": r.score, "preview": r.content[:100]}
                        for r in results
                    ],
                    "tokens_used": result.data.get("usage", {}).get("total_tokens", 0)
                }
            else:
                return {
                    "answer": "Failed to generate response",
                    "error": result.error,
                    "sources": [{"document_id": r.document_id, "score": r.score} for r in results]
                }
                
        except Exception as e:
            logger.exception(f"RAG query failed: {e}")
            return {
                "answer": f"Error: {str(e)}",
                "sources": [],
                "error": True
            }


class UserKnowledgeBaseManager:
    """
    Manages per-user knowledge bases for data isolation.
    """
    
    def __init__(self):
        self._user_kbs: Dict[int, KnowledgeBase] = {}
        self._platform_kb: KnowledgeBase | None = None
    
    def get_user_kb(self, user_id: int) -> KnowledgeBase:
        if user_id not in self._user_kbs:
            logger.info(f"Creating new knowledge base for user {user_id} (lazy-loaded)")
            self._user_kbs[user_id] = KnowledgeBase()
        return self._user_kbs[user_id]
    
    def get_platform_kb(self) -> KnowledgeBase:
        if self._platform_kb is None:
            logger.info("Creating platform knowledge base (lazy-loaded)")
            self._platform_kb = KnowledgeBase()
        return self._platform_kb
    
    def clear_user_kb(self, user_id: int) -> bool:
        if user_id in self._user_kbs:
            self._user_kbs[user_id].clear()
            del self._user_kbs[user_id]
            logger.info(f"Cleared knowledge base for user {user_id}")
            return True
        return False
    
    def get_stats(self) -> Dict:
        return {
            'user_count': len(self._user_kbs),
            'user_ids': list(self._user_kbs.keys()),
            'platform_kb_exists': self._platform_kb is not None,
        }

class SessionKnowledgeBaseManager:
    """
    Manages ephemeral per-session knowledge bases for the Hybrid RAG architecture.
    """
    
    def __init__(self):
        self._session_kbs: Dict[str, KnowledgeBase] = {}
        
    def get_session_kb(self, session_id: str) -> KnowledgeBase:
        if session_id not in self._session_kbs:
            logger.info(f"Creating new ephemeral knowledge base for session {session_id} (lazy-loaded)")
            self._session_kbs[session_id] = KnowledgeBase()
        return self._session_kbs[session_id]
        
    def clear_session_kb(self, session_id: str) -> bool:
        if session_id in self._session_kbs:
            self._session_kbs[session_id].clear()
            del self._session_kbs[session_id]
            logger.info(f"Cleared ephemeral knowledge base for session {session_id}")
            return True
        return False

# Global manager instances
_user_kb_manager: UserKnowledgeBaseManager | None = None
_session_kb_manager: SessionKnowledgeBaseManager | None = None


def get_kb_manager() -> UserKnowledgeBaseManager:
    global _user_kb_manager
    if _user_kb_manager is None:
        _user_kb_manager = UserKnowledgeBaseManager()
    return _user_kb_manager

def get_session_kb_manager() -> SessionKnowledgeBaseManager:
    global _session_kb_manager
    if _session_kb_manager is None:
        _session_kb_manager = SessionKnowledgeBaseManager()
    return _session_kb_manager


def get_user_knowledge_base(user_id: int) -> KnowledgeBase:
    return get_kb_manager().get_user_kb(user_id)

def get_session_knowledge_base(session_id: str) -> KnowledgeBase:
    return get_session_kb_manager().get_session_kb(session_id)


def get_platform_knowledge_base() -> KnowledgeBase:
    return get_kb_manager().get_platform_kb()


def get_knowledge_base() -> KnowledgeBase:
    logger.warning("get_knowledge_base() is deprecated. Use get_user_knowledge_base(user_id) instead.")
    return get_kb_manager().get_user_kb(0)


def get_rag_pipeline(user_id: int | None = None) -> RAGPipeline:
    if user_id is not None:
        kb = get_user_knowledge_base(user_id)
    else:
        kb = get_platform_knowledge_base()
    return RAGPipeline(kb)
