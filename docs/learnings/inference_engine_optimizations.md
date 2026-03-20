# Inference Engine Optimizations

## 1. Global Singleton Embedder (SentenceTransformer)

### Problem
Previously, the `SentenceTransformer` model (`all-MiniLM-L6-v2`) was instantiated on-the-fly inside the `KnowledgeBase.initialize()` method. Since a separate `KnowledgeBase` instance is created per user, the system was repeatedly loading the model from disk into CPU memory for every active user. 

- This caused severe latency spikes on the first RAG query for any given user.
- It wasted ~100MB of RAM per active user. 50 active users would unnecessarily consume ~5GB of RAM just holding redundant copies of the exact same model weights.

### Solution
We implemented a **Global Singleton** (`_global_embedder`) in `Backend/inference/engine.py`. 

Because embedding models are strictly stateless during inference, a single model instance can be loaded once and safely shared across all users and threads. 

```python
_global_embedder = None
_embedder_lock = asyncio.Lock()

async def get_global_embedder():
    """Lazy-load the SentenceTransformer model strictly once globally."""
    global _global_embedder
    ...
```

### Impact
- **Latency**: Reduced from several seconds per new user initialization down to a few milliseconds.
- **Memory**: The model footprint is strictly capped at ~100MB total across the entire Python process, regardless of whether there are 5 or 5,000 active users.

### Tradeoffs
The only tradeoff is CPU concurrency (due to the Python GIL). Multiple users requesting embeddings at the exact same millisecond will be processed sequentially by the single model instance. However, since `all-MiniLM-L6-v2` encodes text in milliseconds, this sequential bottleneck is virtually unnoticeable and far outweighs the cost of loading redundant models.
