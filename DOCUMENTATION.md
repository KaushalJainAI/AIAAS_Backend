# Backend Development Documentation

> Workflow Automation Backend - Complete Feature Reference

---

## Status: ✅ 100% Feature Complete

**Last Updated**: 2026-01-22

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (React)                          │
└─────────────────────────┬───────────────────────────────────────┘
                          │ REST API / WebSocket
┌─────────────────────────┴───────────────────────────────────────┐
│                     Django Backend                               │
├──────────────┬──────────────┬──────────────┬───────────────────┤
│     Core     │  Orchestrator│   Executor   │    Inference       │
│  (Auth/API)  │  (Workflows) │  (Runtime)   │    (RAG)           │
├──────────────┴──────────────┴──────────────┴───────────────────┤
│                        Celery Workers                            │
├─────────────────────────────────────────────────────────────────┤
│                    PostgreSQL + Redis                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## App Structure

| App | Purpose | Key Files |
|-----|---------|-----------|
| `core` | Auth, Users, Security | `authentication.py`, `permissions.py`, `security_config.py` |
| `nodes` | Node System | `handlers/base.py`, `llm_nodes.py`, `integration_nodes.py` |
| `compiler` | Workflow Compilation | `compiler.py`, `validators.py`, `langgraph_builder.py` |
| `executor` | Execution Engine | `runner.py`, `orchestrator.py`, `safe_execution.py` |
| `orchestrator` | Workflows & HITL | `models.py`, `views.py`, `ai_generator.py`, `approval_gates.py` |
| `credentials` | Secret Management | `models.py`, `manager.py` |
| `inference` | RAG Engine | `engine.py`, `views.py` |
| `logs` | Logging & Analytics | `logger.py`, `views.py` |
| `streaming` | Real-time | `consumers.py` |

---

## Key Features

### Node System
- **LLM Nodes**: OpenAI, Gemini, Ollama (local)
- **Integration Nodes**: Gmail, Slack, Google Sheets
- **Core Nodes**: HTTP Request, Code, Set, If/Switch
- **Trigger Nodes**: Manual, Webhook, Schedule
- **Custom Nodes**: Dynamic loading with AST security

### Compiler
- DAG validation (cycles, orphans)
- Type compatibility checking
- Credential validation
- LangGraph builder for orchestration

### Executor
- Retry logic with configurable attempts
- Per-node timeouts
- Error output handles
- Safe sandboxed code execution
- Method whitelisting

### Orchestrator
- Workflow start/stop/pause/resume
- Human-in-the-Loop (HITL) integration
- AI workflow generation
- Context-aware chat
- Version history

### Security
- JWT + API key authentication
- Tier-based rate limiting
- Input sanitization (prompt injection)
- Log sanitization (PII/secrets)
- CORS, CSP, secure cookies
- Abuse detection & blocking
- Thread-safe context isolation

### Real-time
- SSE for execution streaming
- WebSocket for HITL notifications
- Progress tracking

---

## API Endpoints Summary

| Category | Base Path | Endpoints |
|----------|-----------|-----------|
| Core | `/api/` | Auth, Users, API Keys |
| Orchestrator | `/api/orchestrator/` | Workflows, Executions, HITL, AI Chat |
| Logs | `/api/logs/` | Insights, Audit, History |
| Inference | `/api/inference/` | Documents, RAG |
| Nodes | `/api/nodes/` | Registry, Custom Nodes |
| Compiler | `/api/compile/` | Validate, Compile |
| Credentials | `/api/credentials/` | CRUD, Types |
| Streaming | `/api/streaming/` | SSE Events |

See [PERMISSIONS.md](./PERMISSIONS.md) for detailed permissions.

---

## Technical Stack

| Component | Technology |
|-----------|------------|
| Framework | Django 4.2 + DRF |
| Validation | Pydantic |
| Graph Execution | LangGraph |
| Task Queue | Celery + Redis |
| Real-time | Django Channels |
| Vector DB | FAISS |
| Embeddings | sentence-transformers |
| Database | PostgreSQL |

---

## File Reference

### Key Implementation Files

```
orchestrator/
├── ai_generator.py      # AI workflow generation
├── chat_context.py      # Context-aware chat
├── approval_gates.py    # HITL approval system
├── notifications.py     # Email/push notifications
└── views.py            # API endpoints

executor/
├── orchestrator.py      # Workflow orchestration
├── context_isolation.py # Thread-safe isolation
├── safe_execution.py    # Sandboxed code execution
├── dead_letter_queue.py # Failed task management
└── tasks.py            # Celery tasks

core/
└── security_config.py   # Security utilities

inference/
├── engine.py           # RAG pipeline
└── views.py           # Document APIs

nodes/handlers/
├── llm_nodes.py        # OpenAI, Gemini, Ollama
├── integration_nodes.py # Gmail, Slack, Sheets
└── custom_loader.py    # Dynamic node loading

compiler/
└── langgraph_builder.py # LangGraph integration

credentials/
└── manager.py          # Credential management
```

---

## Environment Variables

```bash
# Database
DATABASE_URL=postgres://user:pass@localhost:5432/workflow

# Redis
REDIS_URL=redis://localhost:6379/0

# Security
SECRET_KEY=your-secret-key
ALLOWED_HOSTS=localhost,127.0.0.1

# LLM APIs (optional)
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...

# Email (optional)
EMAIL_HOST=smtp.gmail.com
EMAIL_HOST_USER=...
EMAIL_HOST_PASSWORD=...
```

---

## Running the Backend

```bash
# Install dependencies
pip install -r requirements.txt

# Migrate database
python manage.py migrate

# Run development server
python manage.py runserver

# Run Celery worker (separate terminal)
celery -A workflow_backend worker -l info
```

---

## Testing

```bash
# Run all tests
python manage.py test

# Run specific app
python manage.py test executor

# With coverage
coverage run manage.py test
coverage report
```
