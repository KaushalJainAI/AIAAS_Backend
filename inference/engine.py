"""
Inference Engine - RAG Query Pipeline and Knowledge Base

Implements:
- Knowledge base with FAISS/Chroma vector storage
- Document chunking and embedding
- RAG query pipeline for context-aware LLM responses
"""
import logging
from typing import Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


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
    
    async def initialize(self):
        """Initialize the vector store and embedder."""
        if self._initialized:
            return
        
        try:
            import faiss
            import numpy as np
            from sentence_transformers import SentenceTransformer
            
            # Load embedding model
            self._embedder = SentenceTransformer('all-MiniLM-L6-v2')
            
            # Create FAISS index (384 dimensions for MiniLM)
            self._index = faiss.IndexFlatIP(384)  # Inner product for cosine similarity
            self._initialized = True
            
            logger.info("Knowledge base initialized with FAISS and MiniLM embedder")
            
        except ImportError as e:
            logger.warning(f"FAISS or sentence-transformers not installed: {e}")
            self._initialized = False
    
    async def add_document(
        self,
        doc_id: int,
        content: str,
        metadata: dict | None = None,
        chunk_size: int = 500,
        chunk_overlap: int = 50
        ) -> list[str]:
        """
        Add a document to the knowledge base.
        
        Args:
            doc_id: Document ID in database
            content: Document text content
            metadata: Additional metadata
            chunk_size: Characters per chunk
            chunk_overlap: Overlap between chunks
            
        Returns:
            List of chunk IDs created
        """
        if not self._initialized:
            await self.initialize()
            if not self._initialized:
                return []
        
        import numpy as np
        
        # Chunk the document
        chunks = self._chunk_text(content, chunk_size, chunk_overlap)
        chunk_ids = []
        
        for i, chunk in enumerate(chunks):
            chunk_id = f"doc_{doc_id}_chunk_{i}"
            
            # Generate embedding
            embedding = self._embedder.encode([chunk])[0]
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
        top_k: int = 5,
        min_score: float = 0.3
        ) -> list[SearchResult]:
        """
        Search the knowledge base.
        
        Args:
            query: Search query
            top_k: Number of results to return
            min_score: Minimum similarity score
            
        Returns:
            List of SearchResult objects
        """
        if not self._initialized or self._index.ntotal == 0:
            return []
        
        import numpy as np
        
        # Generate query embedding
        query_embedding = self._embedder.encode([query])[0]
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
            
            doc_id, content, metadata = self._documents.get(idx, (None, "", {}))
            if doc_id is not None:
                results.append(SearchResult(
                    document_id=doc_id,
                    chunk_id=f"chunk_{idx}",
                    content=content,
                    score=float(score),
                    metadata=metadata
                ))
        
        return results
    
    def _chunk_text(
        self,
        text: str,
        chunk_size: int,
        overlap: int
        ) -> list[str]:
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
        """Remove a document from the knowledge base."""
        # Note: FAISS doesn't support deletion directly
        # For production, use a proper vector DB like Chroma or Pinecone
        logger.warning("Document deletion not implemented for FAISS")
        return False
    
    def clear(self):
        """Clear all documents from the knowledge base."""
        if self._index:
            self._index.reset()
        self._documents.clear()
        logger.info("Knowledge base cleared")


class RAGPipeline:
    """
    Retrieval-Augmented Generation pipeline.
    
    Combines knowledge base search with LLM generation.
    
    Usage:
        pipeline = RAGPipeline(kb=knowledge_base)
        response = await pipeline.query(
            question="What is X?",
            llm_node="openai",
            context=execution_context
        )
    """
    
    def __init__(self, knowledge_base: KnowledgeBase):
        self.kb = knowledge_base
    
    async def query(
        self,
        question: str,
        user_id: int,
        llm_type: str = "openai",
        top_k: int = 5,
        credential_id: str | None = None,
        context: Any = None
    ) -> dict:
        """
        Run a RAG query.
        
        Args:
            question: User's question
            user_id: User making the query
            llm_type: LLM to use (openai, gemini, ollama)
            top_k: Number of context chunks to retrieve
            credential_id: LLM credential ID
            context: Execution context
            
        Returns:
            Dict with answer, sources, and metadata
        """
        # Search knowledge base
        results = await self.kb.search(question, top_k=top_k)
        
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


# User Knowledge Base Manager - Provides per-user isolation
class UserKnowledgeBaseManager:
    """
    Manages per-user knowledge bases for data isolation.
    
    Each user has their own isolated FAISS index.
    A separate platform KB exists for opted-in shared documents.
    
    Usage:
        manager = get_kb_manager()
        user_kb = manager.get_user_kb(user_id)
        await user_kb.initialize()
        await user_kb.add_document(...)
    """
    
    def __init__(self):
        self._user_kbs: dict[int, KnowledgeBase] = {}
        self._platform_kb: KnowledgeBase | None = None
        self._lock = None  # For thread safety in async context
    
    def get_user_kb(self, user_id: int) -> KnowledgeBase:
        """
        Get or create a knowledge base for a specific user.
        
        Args:
            user_id: The user's ID
            
        Returns:
            User-specific KnowledgeBase instance
        """
        if user_id not in self._user_kbs:
            logger.info(f"Creating new knowledge base for user {user_id}")
            self._user_kbs[user_id] = KnowledgeBase()
        return self._user_kbs[user_id]
    
    def get_platform_kb(self) -> KnowledgeBase:
        """
        Get the shared platform knowledge base.
        Contains documents that users have opted to share.
        
        Returns:
            Platform-wide shared KnowledgeBase instance
        """
        if self._platform_kb is None:
            logger.info("Creating platform knowledge base")
            self._platform_kb = KnowledgeBase()
        return self._platform_kb
    
    def clear_user_kb(self, user_id: int) -> bool:
        """Clear a user's knowledge base (e.g., when they delete all documents)."""
        if user_id in self._user_kbs:
            self._user_kbs[user_id].clear()
            del self._user_kbs[user_id]
            logger.info(f"Cleared knowledge base for user {user_id}")
            return True
        return False
    
    def get_stats(self) -> dict:
        """Get statistics about knowledge bases."""
        return {
            'user_count': len(self._user_kbs),
            'user_ids': list(self._user_kbs.keys()),
            'platform_kb_exists': self._platform_kb is not None,
        }


# Global manager instance
_kb_manager: UserKnowledgeBaseManager | None = None


def get_kb_manager() -> UserKnowledgeBaseManager:
    """Get the global knowledge base manager instance."""
    global _kb_manager
    if _kb_manager is None:
        _kb_manager = UserKnowledgeBaseManager()
    return _kb_manager


def get_user_knowledge_base(user_id: int) -> KnowledgeBase:
    """Convenience function to get a user's knowledge base."""
    return get_kb_manager().get_user_kb(user_id)


def get_platform_knowledge_base() -> KnowledgeBase:
    """Convenience function to get the platform knowledge base."""
    return get_kb_manager().get_platform_kb()


# Legacy compatibility - will use user_id=0 as fallback
# DEPRECATED: Use get_user_knowledge_base(user_id) instead
def get_knowledge_base() -> KnowledgeBase:
    """
    DEPRECATED: Use get_user_knowledge_base(user_id) for proper isolation.
    This returns a fallback KB and logs a warning.
    """
    logger.warning("get_knowledge_base() is deprecated. Use get_user_knowledge_base(user_id) instead.")
    return get_kb_manager().get_user_kb(0)  # Fallback to user 0


def get_rag_pipeline(user_id: int | None = None) -> RAGPipeline:
    """
    Get RAG pipeline for a specific user.
    
    Args:
        user_id: User ID for user-specific KB. If None, uses platform KB.
        
    Returns:
        RAGPipeline configured with appropriate knowledge base
    """
    if user_id is not None:
        kb = get_user_knowledge_base(user_id)
    else:
        kb = get_platform_knowledge_base()
    return RAGPipeline(kb)

