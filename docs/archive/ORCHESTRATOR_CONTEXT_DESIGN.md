# Orchestrator Continuous Context - Design Document

> **Purpose**: Enable the orchestrator to maintain persistent memory and context across conversations, sessions, and workflow executions.

> **Last Updated**: 2026-02-03

> **Implementation Status**: 
> - ✅ Unified KingOrchestrator with LLM capabilities
> - ✅ Goal-oriented execution with RuntimeContext
> - ✅ DesignTimeContext for workflow generation
> - ⬜ OrchestratorThread model (pending)
> - ⬜ Audio transcription pipeline (future)

---

## Overview

The orchestrator now operates with clear separation between:
- **Design-Time Context** (Knowledge Base) - For workflow generation/modification
- **Runtime Context** (Goal-Based) - For workflow execution control

This document proposes a hierarchical memory system that enables:

- **Conversation continuity** within sessions
- **Cross-session recall** of user preferences and patterns
- **Execution state persistence** for pause/resume
- **Semantic memory** for intelligent context retrieval

---

## Memory Architecture

### Three-Tier Hierarchy

```
┌─────────────────────────────────────────────────────────────┐
│                    LONG-TERM MEMORY                        │
│              (ChromaDB - Vector Database)                   │
│   • User patterns and preferences                          │
│   • Successful workflow templates                          │
│   • Historical conversation summaries                      │
│   • Semantic search for relevant past context              │
├─────────────────────────────────────────────────────────────┤
│                    SESSION MEMORY                          │
│                   (PostgreSQL - JSON)                       │
│   • Conversation thread with messages                      │
│   • Compressed summaries of older context                  │
│   • Current execution state snapshots                      │
├─────────────────────────────────────────────────────────────┤
│                    WORKING MEMORY                          │
│               (Redis / In-Process Cache)                    │
│   • Active LLM conversation window                         │
│   • Current node execution state                           │
│   • Real-time HITL pending requests                        │
└─────────────────────────────────────────────────────────────┘
```

| Tier | Storage | Lifespan | Access Speed | Use Case |
|------|---------|----------|--------------|----------|
| Working | Redis/Memory | Single execution | ~1ms | Current conversation window |
| Session | PostgreSQL | Days/weeks | ~10ms | Conversation history |
| Long-term | ChromaDB | Permanent | ~50ms | Semantic recall |

---

## Data Models

### OrchestratorThread (PostgreSQL)

Tracks conversation state within a session.

```python
class OrchestratorThread(models.Model):
    """Persistent conversation thread for orchestrator context."""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    workflow = models.ForeignKey(Workflow, null=True, blank=True, on_delete=models.SET_NULL)
    
    # Conversation history
    messages = models.JSONField(default=list)
    # Format: [{"role": "user|assistant|system", "content": "...", "timestamp": "..."}]
    
    # Compressed summary of older messages
    context_summary = models.TextField(blank=True)
    
    # Token tracking for context window management
    token_count = models.IntegerField(default=0)
    max_tokens = models.IntegerField(default=8000)  # Conservative limit
    
    # Metadata
    title = models.CharField(max_length=200, blank=True)
    last_intent = models.CharField(max_length=500, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-updated_at']
```

### OrchestratorMemory (ChromaDB Collection)

Schema for vector-stored memories.

```python
# Collection: "orchestrator_memories"
# Each document contains:

{
    "id": "uuid",
    "document": "Summarized context or insight text",
    "metadata": {
        "user_id": 123,
        "workflow_id": 456,      # Optional
        "memory_type": "intent|pattern|error|success|preference",
        "timestamp": "2026-02-03T15:00:00Z",
        "relevance_score": 0.85   # Self-assessed importance
    }
}
```

---

## Context Injection Strategy

### Prompt Construction Order

When calling the LLM, construct the context in this order:

```
1. [SYSTEM PROMPT]
   - Role definition
   - Available node types
   - User preferences (from long-term memory)

2. [RETRIEVED CONTEXT] (ChromaDB semantic search)
   - Top 3 relevant past interactions
   - Similar workflow patterns

3. [SESSION SUMMARY]
   - LLM-generated summary of older conversation
   - Only used when thread exceeds token threshold

4. [RECENT MESSAGES]
   - Last 5-10 messages from current thread
   - Full content, not summarized

5. [CURRENT PROMPT]
   - User's immediate request
   - Current execution state if applicable
```

### Example Assembled Context

```
[SYSTEM]
You are an AI workflow assistant for user #123.
User preferences: prefers concise responses, commonly uses Gmail and Slack nodes.

[RETRIEVED - Similar past interaction]
Previously, this user asked to "send weekly reports via email" and we created
a workflow with Schedule -> Google Sheets -> Gmail nodes.

[SESSION SUMMARY]
Earlier in this session, the user asked about connecting to Salesforce.
We discussed API credentials and decided to use the HTTP node with OAuth.

[RECENT MESSAGES]
User: "Now add a step to log all processed records"
Assistant: "I'll add a Set node to capture the record data, then..."
User: "Actually, store it in Google Sheets instead"

[CURRENT PROMPT]
User: "Also send me a Slack notification when it's done"
```

---

## Context Window Management

### Sliding Window with Summarization

When `token_count` approaches `max_tokens`:

```
┌─────────────────────────────────────────────────────────────┐
│  BEFORE (Token count: 7500/8000)                           │
│                                                            │
│  [System] + [Msg1] + [Msg2] + [Msg3] + ... + [Msg20]      │
└─────────────────────────────────────────────────────────────┘
                              ↓
                    Summarization Triggered
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  AFTER (Token count: 3000/8000)                            │
│                                                            │
│  [System] + [SUMMARY of Msg1-15] + [Msg16-20]             │
└─────────────────────────────────────────────────────────────┘
```

### Summarization Prompt

```
Summarize the following conversation history into key points.
Preserve: decisions made, user preferences expressed, technical details.
Discard: pleasantries, repeated clarifications, superseded information.

Conversation:
{older_messages}

Summary (be concise):
```

---

## Execution State Persistence

### Checkpoint System

Extend `ExecutionHandle` to support context snapshots:

```python
@dataclass
class ExecutionHandle:
    execution_id: UUID
    # ... existing fields ...
    
    # Context persistence
    context_snapshot: dict = field(default_factory=dict)
    # Contains:
    # - thread_id: UUID of associated OrchestratorThread
    # - last_messages: Recent conversation for quick resume
    # - variable_state: Current workflow variables
```

### Pause/Resume Flow

```
PAUSE:
  1. Serialize current conversation to context_snapshot
  2. Save ExecutionHandle to database
  3. Store thread_id reference

RESUME:
  1. Load ExecutionHandle from database
  2. Restore conversation from context_snapshot
  3. Inject "Resuming from pause..." context message
  4. Continue execution
```

---

## ChromaDB Integration

### Memory Collection Setup

```python
# In orchestrator/memory.py

from chromadb import Client
from chromadb.config import Settings

def get_memory_collection():
    """Get or create the orchestrator memories collection."""
    client = Client(Settings(
        chroma_db_impl="duckdb+parquet",
        persist_directory="./chromadb_data"
    ))
    return client.get_or_create_collection(
        name="orchestrator_memories",
        metadata={"description": "Long-term orchestrator context"}
    )
```

### Memory Operations

```python
async def store_memory(user_id: int, content: str, memory_type: str, workflow_id: int = None):
    """Store a new memory in long-term storage."""
    collection = get_memory_collection()
    collection.add(
        documents=[content],
        metadatas=[{
            "user_id": user_id,
            "workflow_id": workflow_id,
            "memory_type": memory_type,
            "timestamp": datetime.utcnow().isoformat()
        }],
        ids=[str(uuid4())]
    )

async def recall_memories(user_id: int, query: str, limit: int = 3) -> list[str]:
    """Retrieve relevant memories using semantic search."""
    collection = get_memory_collection()
    results = collection.query(
        query_texts=[query],
        n_results=limit,
        where={"user_id": user_id}
    )
    return results["documents"][0] if results["documents"] else []
```

---

## User Profile Context

### Profile Structure

Store in `User.profile` or as workflow_settings extension:

```python
{
    "orchestrator_preferences": {
        "response_style": "concise",      # or "detailed"
        "default_llm_provider": "openrouter",
        "common_integrations": ["gmail", "slack", "google_sheets"],
        "workflow_patterns": ["data-sync", "notifications", "approvals"],
        "timezone": "Asia/Kolkata"
    },
    "recent_intents": [
        "Send weekly sales report via email",
        "Sync contacts from Salesforce to HubSpot",
        "Alert on new GitHub issues"
    ]
}
```

### Profile Injection

Include in system prompt:

```
User Profile:
- Prefers concise responses
- Frequently uses: Gmail, Slack, Google Sheets
- Common patterns: data synchronization, notifications
- Timezone: Asia/Kolkata (IST)
```

---

## Implementation Phases

### Phase 1: Session Persistence (Week 1)
- [ ] Create `OrchestratorThread` model
- [ ] Migrate database
- [ ] Update `AIWorkflowGenerator` to load/save threads
- [ ] Add thread cleanup job (delete threads older than 30 days)

### Phase 2: Automatic Summarization (Week 2)
- [ ] Implement token counting utility
- [ ] Add summarization trigger at 75% capacity
- [ ] Store summaries in `context_summary` field
- [ ] Update prompt construction to use summaries

### Phase 3: Semantic Memory (Week 3)
- [ ] Create ChromaDB collection for memories
- [ ] Implement `store_memory()` and `recall_memories()`
- [ ] Auto-store successful workflow patterns
- [ ] Inject retrieved memories into prompt

### Phase 4: User Profiles (Week 4)
- [ ] Extend User model with `orchestrator_preferences`
- [ ] Build preference learning from conversation history
- [ ] Inject profile into system prompts

---

## API Endpoints

### Thread Management

```
GET    /api/orchestrator/threads/                 # List user's threads
POST   /api/orchestrator/threads/                 # Create new thread
GET    /api/orchestrator/threads/{id}/            # Get thread with messages
POST   /api/orchestrator/threads/{id}/messages/   # Add message to thread
DELETE /api/orchestrator/threads/{id}/            # Delete thread
```

### Memory Recall

```
POST   /api/orchestrator/recall/
Body: { "query": "email notification workflow", "limit": 5 }
Response: { "memories": ["...", "...", "..."] }
```

---

## Compatibility Notes

| Component | Integration Point |
|-----------|-------------------|
| `AIWorkflowGenerator` | Inject context in `_call_llm()` |
| `ExecutionContext` | Add `thread_id` field |
| `KingAgent` | Use thread for HITL conversations |
| `ChromaDB` | Already set up for templates |
| LLM Providers | Works with any configured provider |

---

## Token Budget Guidelines

| Context Section | Recommended Tokens | Priority |
|-----------------|-------------------|----------|
| System prompt | 500-1000 | Must have |
| Retrieved memories | 500-1000 | High |
| Session summary | 500-1000 | Medium |
| Recent messages | 2000-4000 | High |
| Current prompt | 500-1500 | Must have |
| **Reserved for response** | **2000-4000** | Must have |

Total: ~8000-12000 tokens recommended context window

---

## Future Enhancements

1. **Multi-user collaboration context** - Share context between team members
2. **Workflow-specific memory** - Context scoped to specific workflows
3. **Automatic learning** - Extract patterns from successful executions
4. **Context compression** - Use embedding-based compression for efficiency
5. **Federated memory** - Sync across multiple orchestrator instances
6. **Audio transcription pipeline** - Continuous voice input with context extraction (see below)

---

## Future: Audio Transcription Context Pipeline

> **Status**: Planned for later implementation. Current architecture is designed to support this.

### Overview

Enable continuous audio input (voice commands, meetings, calls) to be transcribed, summarized, and injected into orchestrator context in real-time.

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Audio     │───▶│ Transcriber │───▶│ Summarizer  │───▶│ Orchestrator│
│   Stream    │    │  (STT API)  │    │   (LLM)     │    │   Context   │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
     │                   │                   │                   │
     ▼                   ▼                   ▼                   ▼
  WebSocket         Raw text            Key points         OrchestratorThread
  or chunks         transcript          + intents          or ChromaDB
```

### Why Current Design Supports This

| Current Component | Audio Extension |
|-------------------|-----------------|
| `OrchestratorThread.messages` | Store transcription chunks as `role: "audio"` |
| `context_summary` | Store rolling audio summary |
| ChromaDB memories | Index audio-derived insights for recall |
| Token management | Same sliding window works for audio context |
| LLM abstraction | Same providers can do summarization |

### Proposed Data Model Extension

```python
class AudioTranscript(models.Model):
    """Streaming audio transcription with context extraction."""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    thread = models.ForeignKey(OrchestratorThread, on_delete=models.CASCADE)
    
    # Source metadata
    source_type = models.CharField(max_length=50)  # "microphone", "call", "meeting"
    source_id = models.CharField(max_length=200, blank=True)  # Meeting ID, call ID
    
    # Transcription
    raw_text = models.TextField()  # Full transcript
    chunks = models.JSONField(default=list)
    # Format: [{"text": "...", "timestamp": 1.5, "speaker": "user"}]
    
    # Extracted context (LLM-processed)
    summary = models.TextField(blank=True)
    extracted_intents = models.JSONField(default=list)  # ["schedule meeting", "send report"]
    extracted_entities = models.JSONField(default=dict)  # {"emails": [], "dates": [], "names": []}
    action_items = models.JSONField(default=list)  # ["Email John by Friday"]
    
    # State
    is_processing = models.BooleanField(default=True)
    duration_seconds = models.FloatField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
```

### Processing Pipeline

```python
# Future: orchestrator/audio_pipeline.py

class AudioContextPipeline:
    """Process streaming audio into orchestrator context."""
    
    CHUNK_DURATION_SECONDS = 30  # Process every 30 seconds
    SUMMARY_THRESHOLD_TOKENS = 2000  # Summarize when transcript grows
    
    async def process_chunk(self, thread_id: UUID, audio_chunk: bytes) -> dict:
        """Process a single audio chunk."""
        
        # 1. Transcribe (using configured LLM/STT provider)
        text = await self.transcribe(audio_chunk)
        
        # 2. Append to transcript
        transcript = await self.append_to_transcript(thread_id, text)
        
        # 3. Extract intents/entities (lightweight NER)
        entities = await self.extract_entities(text)
        
        # 4. If threshold reached, summarize
        if transcript.token_count > self.SUMMARY_THRESHOLD_TOKENS:
            summary = await self.summarize(transcript)
            await self.update_thread_context(thread_id, summary)
        
        return {"text": text, "entities": entities}
    
    async def finalize_session(self, thread_id: UUID) -> dict:
        """Called when audio session ends."""
        
        # 1. Generate final summary
        transcript = await self.get_transcript(thread_id)
        summary = await self.generate_session_summary(transcript)
        
        # 2. Extract action items
        action_items = await self.extract_action_items(transcript)
        
        # 3. Store in long-term memory (ChromaDB)
        await self.store_in_memory(thread_id, summary, action_items)
        
        # 4. Update thread context for orchestrator
        await self.inject_into_thread(thread_id, summary, action_items)
        
        return {"summary": summary, "action_items": action_items}
```

### Context Injection for Audio

When audio is active, modify the prompt construction:

```
[SYSTEM PROMPT]
Note: User has active audio input. Recent voice context:

[AUDIO SUMMARY]
"User discussed sending weekly sales reports to the marketing team.
Mentioned John from finance should be CC'd. Deadline is Friday."

[EXTRACTED ACTION ITEMS]
- Send weekly sales report to marketing
- CC John from finance
- Complete by Friday

[RECENT MESSAGES]
...
```

### Streaming Architecture (WebSocket)

```python
# Future: orchestrator/consumers.py

class AudioTranscriptionConsumer(AsyncJsonWebsocketConsumer):
    """WebSocket consumer for real-time audio streaming."""
    
    async def receive_json(self, content):
        if content["type"] == "audio_chunk":
            # Process base64-encoded audio
            audio_data = base64.b64decode(content["data"])
            result = await self.pipeline.process_chunk(
                thread_id=self.thread_id,
                audio_chunk=audio_data
            )
            
            # Send back transcription
            await self.send_json({
                "type": "transcription",
                "text": result["text"],
                "entities": result["entities"]
            })
        
        elif content["type"] == "end_session":
            result = await self.pipeline.finalize_session(self.thread_id)
            await self.send_json({
                "type": "session_complete",
                "summary": result["summary"],
                "action_items": result["action_items"]
            })
```

### STT Provider Abstraction

```python
# Support multiple speech-to-text backends

class STTProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio: bytes) -> str:
        pass

class WhisperSTT(STTProvider):
    """OpenAI Whisper API"""
    async def transcribe(self, audio: bytes) -> str:
        # Use OpenAI Whisper
        pass

class GoogleSTT(STTProvider):
    """Google Cloud Speech-to-Text"""
    async def transcribe(self, audio: bytes) -> str:
        pass

class LocalWhisperSTT(STTProvider):
    """Local Whisper model via faster-whisper"""
    async def transcribe(self, audio: bytes) -> str:
        pass
```

### Design Decisions for Compatibility

1. **Same memory store**: Audio context uses the same `OrchestratorThread` and ChromaDB
2. **Same LLM abstraction**: Summarization uses the configured `llm_provider`
3. **Same token management**: Audio summaries follow the same sliding window rules
4. **Same message format**: Audio adds `role: "audio_context"` messages to thread
5. **Same retrieval**: ChromaDB can recall both text and audio-derived memories

### Implementation Checklist (Future)

- [ ] Add `AudioTranscript` model
- [ ] Create STT provider abstraction
- [ ] Implement chunk processing pipeline
- [ ] Add WebSocket consumer for streaming
- [ ] Build summarization and entity extraction
- [ ] Integrate with OrchestratorThread
- [ ] Add frontend audio recording component
- [ ] Support meeting integrations (Zoom, Meet, Teams)

---

## Cost Analysis & Provider Recommendations

> **Objective**: Build a cost-effective continuous context manager using the two-tier approach:
> 1. Short-term (last hour) in PostgreSQL
> 2. Long-term in ChromaDB with semantic search

### Speech-to-Text (Transcription) Costs

| Provider | Model | Cost | Quality | Notes |
|----------|-------|------|---------|-------|
| **OpenAI Whisper** | whisper-1 | **$0.006/min** | Excellent | Best quality/cost ratio |
| Deepgram | nova-2 | $0.0043/min | Very good | Slightly cheaper |
| AssemblyAI | Best | $0.01/min | Excellent | Premium features |
| **Local Whisper** | faster-whisper | **$0** | Excellent | Requires ~4GB VRAM GPU |

**Cost for 8 hours/day of audio:**
- Whisper API: `480 min × $0.006 = ~$2.88/day = ~$86/month`
- Local Whisper: **$0** (recommended if GPU available)

### Summarization (LLM) Costs

| Provider | Model | Input Cost | Output Cost | Quality |
|----------|-------|------------|-------------|---------|
| **OpenRouter (Free)** | gemini-2.0-flash-exp:free | $0 | $0 | Good |
| **OpenRouter (Free)** | llama-3.3-70b:free | $0 | $0 | Good |
| **OpenRouter (Free)** | deepseek-chat:free | $0 | $0 | Good |
| DeepSeek | deepseek-chat | $0.14/1M in | $0.28/1M out | Great |
| Gemini | gemini-1.5-flash | $0.075/1M in | $0.30/1M out | Very good |
| GPT-4o-mini | gpt-4o-mini | $0.15/1M in | $0.60/1M out | Very good |

**Cost for summarizing 1 hour of conversation (~13,000 tokens):**
- Free tier: **$0**
- DeepSeek: ~$0.002
- GPT-4o-mini: ~$0.003

**Cost for 16 summarizations/day (every 30 min for 8 hours):**
- Free tier: **$0**
- DeepSeek: ~$0.03/day = ~$1/month
- GPT-4o-mini: ~$0.05/day = ~$1.50/month

### Embedding Costs

| Provider | Model | Cost | Dimensions |
|----------|-------|------|------------|
| **Local (sentence-transformers)** | all-MiniLM-L6-v2 | **$0** | 384 |
| OpenAI | text-embedding-3-small | $0.02/1M tokens | 1536 |
| Voyage | voyage-lite-02-instruct | $0.02/1M tokens | 1024 |

**Cost for 16 summaries/day (~4,000 tokens total):**
- Local: **$0**
- OpenAI: $0.00008 (basically free)

### Total Daily/Monthly Cost Estimates

| Scenario | STT | LLM | Embedding | **Daily** | **Monthly** |
|----------|-----|-----|-----------|-----------|-------------|
| **Budget (All Local/Free)** | $0 | $0 | $0 | **$0** | **$0** |
| **Budget (API STT)** | $2.88 | $0 | $0 | ~$3 | **~$90** |
| **Balanced** | $2.88 | $0.03 | $0 | ~$3 | **~$90** |
| **Premium** | $4.80 | $0.50 | $0.02 | ~$5.30 | **~$160** |

### Recommended Stack (Cost-Optimized)

```
┌─────────────────────────────────────────────────────────────┐
│  RECOMMENDED: $0/month (requires GPU)                       │
├─────────────────────────────────────────────────────────────┤
│  STT:        Local faster-whisper (free, ~4GB VRAM)        │
│  LLM:        OpenRouter free tier (Gemini Flash, Llama 3)  │
│  Embeddings: sentence-transformers (local, free)           │
│  Short-term: PostgreSQL (you have this)                    │
│  Long-term:  ChromaDB (you have this)                      │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  ALTERNATIVE: ~$90/month (no GPU needed)                    │
├─────────────────────────────────────────────────────────────┤
│  STT:        OpenAI Whisper API ($0.006/min)               │
│  LLM:        OpenRouter free tier                          │
│  Embeddings: Local sentence-transformers                   │
│  Short-term: PostgreSQL                                    │
│  Long-term:  ChromaDB                                      │
└─────────────────────────────────────────────────────────────┘
```

---

## Database Recommendations

### Short-Term Storage (Last Hour): PostgreSQL

**Why PostgreSQL (your current DB):**

| Advantage | Details |
|-----------|---------|
| ✅ Already set up | No new infrastructure needed |
| ✅ JSON support | `JSONField` for flexible message storage |
| ✅ Transactions | ACID compliance for reliable writes |
| ✅ Timestamp queries | Efficient filtering by time window |
| ✅ No additional cost | Uses existing database |

**Implementation:**
```python
# Filter last hour of context
from django.utils import timezone
from datetime import timedelta

one_hour_ago = timezone.now() - timedelta(hours=1)
recent_messages = OrchestratorThread.objects.filter(
    user=user,
    updated_at__gte=one_hour_ago
).order_by('-updated_at')
```

### Long-Term Storage (Vector DB): ChromaDB

**Why ChromaDB (you already have it):**

| Advantage | Details |
|-----------|---------|
| ✅ Already integrated | Used for template search |
| ✅ Zero cost | Runs locally, no API costs |
| ✅ Semantic search | Find relevant past context |
| ✅ Python native | Easy Django integration |
| ✅ Scales well | Handles ~1M vectors efficiently |

**Alternatives (for future scale):**

| Database | When to Use | Cost |
|----------|-------------|------|
| **ChromaDB** | < 1M vectors, single server | Free |
| **Qdrant** | > 1M vectors, high performance | Free (self-hosted) |
| **Pinecone** | Managed, infinite scale | $70+/month |
| **pgvector** | Want everything in PostgreSQL | Free |

**Recommendation**: Start with **ChromaDB**. Migrate to Qdrant if you exceed 1M memories.

### Data Flow Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    AUDIO INPUT                              │
│                  (Microphone/Meeting)                        │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    STT TRANSCRIPTION                        │
│              (Local Whisper or API)                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│               SHORT-TERM: PostgreSQL                        │
│                                                             │
│  OrchestratorThread                                         │
│  ├── messages: [{role, content, timestamp}, ...]           │
│  ├── context_summary: "Rolling summary..."                  │
│  └── token_count: 3500                                      │
│                                                             │
│  Retention: Last 1 hour (configurable)                      │
│  Query: ORDER BY timestamp DESC LIMIT N                     │
└─────────────────────────────────────────────────────────────┘
                              │
              (Every 30 min or on hour boundary)
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 SUMMARIZATION (LLM)                         │
│            (OpenRouter free tier recommended)               │
│                                                             │
│  Input: Last 30 min of conversation                         │
│  Output: Compressed summary + extracted entities            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              LONG-TERM: ChromaDB                            │
│                                                             │
│  Collection: orchestrator_memories                          │
│  ├── document: "Summary of conversation..."                 │
│  ├── embedding: [0.23, -0.45, ...]  (384 dims)             │
│  └── metadata: {user_id, timestamp, type}                  │
│                                                             │
│  Retention: Permanent                                       │
│  Query: Semantic similarity search                          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│               CONTEXT RETRIEVAL                             │
│                                                             │
│  1. Recent context from PostgreSQL (last hour)             │
│  2. Relevant past from ChromaDB (semantic search)           │
│  3. Assembled into LLM prompt                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Alignment Check ✅

| Requirement | Solution | Status |
|-------------|----------|--------|
| Continuous audio input | WebSocket + STT pipeline | ✅ Designed |
| Last hour context | PostgreSQL + OrchestratorThread | ✅ Compatible |
| Long-term memory | ChromaDB vector store | ✅ Already exists |
| Low cost | Free tier LLMs + local embeddings | ✅ $0-90/month |
| Works with current project | Django + existing stack | ✅ No new deps |
| Summarization | LLM abstraction (any provider) | ✅ Reuses llm_nodes |
| User can choose LLM | llm_provider field on Workflow | ✅ Implemented |

**The architecture is fully aligned with your goals!**
