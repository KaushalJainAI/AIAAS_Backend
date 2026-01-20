# Database Schema Documentation

> Auto-generated schema for Workflow Automation Backend

---

## Entity Relationship Diagram

```mermaid
erDiagram
    User ||--o| UserProfile : has
    User ||--o{ APIKey : owns
    User ||--o{ UsageTracking : tracks
    User ||--o{ CustomNode : creates
    User ||--o{ Credential : owns
    User ||--o{ Workflow : owns
    User ||--o{ Document : uploads
    User ||--o{ ConversationMessage : sends
    User ||--o{ HITLRequest : receives
    
    CredentialType ||--o{ Credential : categorizes
    
    Workflow ||--o{ WorkflowVersion : versions
    Workflow ||--o{ ExecutionLog : executes
    Workflow ||--o{ ConversationMessage : context
    Workflow ||--o{ AuditEntry : audits
    
    ExecutionLog ||--o{ NodeExecutionLog : contains
    ExecutionLog ||--o{ HITLRequest : triggers
    ExecutionLog ||--o{ StreamEvent : emits
    ExecutionLog ||--o{ AuditEntry : logs
    
    Document ||--o{ DocumentChunk : chunks
```

---

## Models by App

### 👤 core

| Model | Description |
|-------|-------------|
| **UserProfile** | Extended user data with tier, limits, credits |
| **APIKey** | Programmatic access keys with rotation |
| **UsageTracking** | Daily usage metrics per user |

### 🧩 nodes

| Model | Description |
|-------|-------------|
| **CustomNode** | User-created custom node handlers |

### 🔐 credentials

| Model | Description |
|-------|-------------|
| **CredentialType** | Integration types (OAuth, API Key, etc.) |
| **Credential** | Encrypted user credentials |

### 📋 logs

| Model | Description |
|-------|-------------|
| **ExecutionLog** | Workflow execution records |
| **NodeExecutionLog** | Per-node execution details |
| **AuditEntry** | HITL decisions and sensitive actions |

### 🤖 orchestrator

| Model | Description |
|-------|-------------|
| **Workflow** | User workflow definitions |
| **WorkflowVersion** | Version history snapshots |
| **HITLRequest** | Human-in-the-loop requests |
| **ConversationMessage** | AI chat history |

### 🧠 inference

| Model | Description |
|-------|-------------|
| **Document** | Uploaded files for RAG |
| **DocumentChunk** | Chunked text with embeddings |

### 📡 streaming

| Model | Description |
|-------|-------------|
| **StreamEvent** | Persisted SSE/WebSocket events |

---

## Field Details

### UserProfile
| Field | Type | Description |
|-------|------|-------------|
| user | FK(User) | OneToOne link |
| tier | CharField | free/pro/enterprise |
| compile_limit | IntegerField | Rate limit per minute |
| execute_limit | IntegerField | Rate limit per minute |
| stream_connections | IntegerField | Max concurrent streams |
| credits_remaining | IntegerField | Available credits |
| credits_used_total | IntegerField | Historical usage |

### APIKey
| Field | Type | Description |
|-------|------|-------------|
| user | FK(User) | Owner |
| name | CharField | Friendly name |
| key | CharField | Auto-generated, unique |
| key_prefix | CharField | First 8 chars for ID |
| is_active | BooleanField | Active status |
| expires_at | DateTimeField | Optional expiration |
| last_used_at | DateTimeField | Last usage timestamp |

### Workflow
| Field | Type | Description |
|-------|------|-------------|
| user | FK(User) | Owner |
| name | CharField | Display name |
| slug | SlugField | URL-safe identifier |
| nodes | JSONField | Array of node definitions |
| edges | JSONField | Array of connections |
| viewport | JSONField | Canvas state |
| settings | JSONField | Workflow config |
| status | CharField | draft/active/paused/archived |
| is_template | BooleanField | Template flag |
| execution_count | IntegerField | Total runs |

### ExecutionLog
| Field | Type | Description |
|-------|------|-------------|
| execution_id | UUIDField | Unique identifier |
| workflow | FK(Workflow) | Executed workflow |
| user | FK(User) | Executor |
| status | CharField | pending/running/completed/failed |
| trigger_type | CharField | manual/schedule/webhook/api |
| started_at | DateTimeField | Start time |
| completed_at | DateTimeField | End time |
| duration_ms | IntegerField | Duration in ms |
| input_data | JSONField | Input payload |
| output_data | JSONField | Final output |
| error_message | TextField | Error if failed |
| nodes_executed | IntegerField | Count |
| tokens_used | IntegerField | LLM tokens |
| credits_used | IntegerField | Credits consumed |

### Credential
| Field | Type | Description |
|-------|------|-------------|
| user | FK(User) | Owner |
| credential_type | FK(CredentialType) | Type |
| name | CharField | Friendly name |
| encrypted_data | BinaryField | Fernet encrypted |
| is_active | BooleanField | Active status |
| is_verified | BooleanField | Verified working |
| last_used_at | DateTimeField | Last usage |

### HITLRequest
| Field | Type | Description |
|-------|------|-------------|
| request_id | UUIDField | Unique identifier |
| execution | FK(ExecutionLog) | Parent execution |
| user | FK(User) | Recipient |
| request_type | CharField | approval/clarification/error_recovery |
| title | CharField | Short title |
| message | TextField | Detailed message |
| options | JSONField | Available choices |
| status | CharField | pending/approved/rejected/timeout |
| response | JSONField | User response |
| timeout_seconds | IntegerField | Timeout config |

### Document
| Field | Type | Description |
|-------|------|-------------|
| document_id | UUIDField | Unique identifier |
| user | FK(User) | Owner |
| name | CharField | Filename |
| file | FileField | Uploaded file |
| file_type | CharField | pdf/txt/md/docx/csv/json/html |
| status | CharField | pending/processing/indexed/failed |
| content_text | TextField | Extracted text |
| chunk_count | IntegerField | Number of chunks |

---

## Statistics

| Metric | Value |
|--------|-------|
| **Total Models** | 16 |
| **Apps with Models** | 7 |
| **Total Fields** | ~120 |
