# API Permissions Documentation

> Complete authorization requirements for all backend endpoints

---

## Permission Levels

| Level | Code | Description |
|-------|------|-------------|
| **Public** | `AllowAny` | No authentication required |
| **Authenticated** | `IsAuthenticated` | Valid JWT or API key required |
| **Owner** | `IsOwner` | Must own the resource |
| **Admin** | `IsAdminUser` | Staff/superuser only |
| **Pro Tier** | `IsProTier` | Pro subscription required |
| **Enterprise** | `IsEnterpriseTier` | Enterprise subscription required |

---

## 👤 Core APIs (`/api/`)

### Authentication
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/auth/register` | POST | Sync | Public | Rate limit: 5/hour |
| `/api/auth/login` | POST | Sync | Public | Rate limit: 10/min |
| `/api/auth/google` | POST | Sync | Public | OAuth2 Flow |
| `/api/auth/refresh` | POST | Sync | Authenticated | Refresh JWT token |
| `/api/auth/logout` | POST | Sync | Authenticated | Invalidate token |

### User Profile
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/users/me` | GET | Sync | Authenticated | Own profile only |
| `/api/users/me` | PATCH | Sync | Authenticated | Own profile only |
| `/api/users/password` | POST | Sync | Authenticated | Change password |
| `/api/users/{id}` | GET | Sync | Admin | View any user |
| `/api/users/` | GET | Sync | Admin | List all users |

### API Keys
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/keys/` | GET | Sync | Authenticated | List own keys |
| `/api/keys/` | POST | Sync | Authenticated | Create new key |
| `/api/keys/{id}` | DELETE | Sync | Owner | Delete own key |
| `/api/keys/{id}/rotate` | POST | Sync | Owner | Rotate key |

### Usage
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/usage/` | GET | Sync | Authenticated | Usage statistics |

---

## 🤖 Orchestrator APIs (`/api/orchestrator/`)

### Workflows CRUD
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/orchestrator/workflows/` | GET | Sync | Authenticated | List own workflows |
| `/api/orchestrator/workflows/` | POST | Sync | Authenticated | Create workflow |
| `/api/orchestrator/workflows/{id}/` | GET | Sync | Owner | View workflow |
| `/api/orchestrator/workflows/{id}/` | PUT | Sync | Owner | Update workflow (Auto-versioning) |
| `/api/orchestrator/workflows/{id}/` | DELETE | Sync | Owner | Delete workflow |
| `/api/orchestrator/workflows/{id}/clone/` | POST | Sync | Owner | Clone workflow |

### Version History
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/orchestrator/workflows/{id}/versions/` | GET | Sync | Owner | List versions |
| `/api/orchestrator/workflows/{id}/versions/` | POST | Sync | Owner | Create snapshot |
| `/api/orchestrator/workflows/{id}/versions/{v}/restore/` | POST | Sync | Owner | Restore version |

### Execution Control
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/orchestrator/workflows/{id}/execute/` | POST | **Async** | Owner | Start execution |
| `/api/orchestrator/executions/{id}/status/` | GET | Sync | Owner | Get status |
| `/api/orchestrator/executions/{id}/pause/` | POST | **Async** | Owner | Pause execution |
| `/api/orchestrator/executions/{id}/resume/` | POST | **Async** | Owner | Resume execution |
| `/api/orchestrator/executions/{id}/stop/` | POST | **Async** | Owner | Stop execution |
| `/api/orchestrator/workflows/{id}/test/` | POST | Sync | Owner | Run background test |

### HITL (Human-in-the-Loop)
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/orchestrator/hitl/pending/` | GET | Sync | Authenticated | Pending requests |
| `/api/orchestrator/hitl/{id}/respond/` | POST | **Async** | Owner | Submit response |

### AI Chat
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/orchestrator/chat/` | GET | Sync | Authenticated | List conversations |
| `/api/orchestrator/chat/` | POST | Sync | Authenticated | Send message |
| `/api/orchestrator/chat/{id}/` | GET | Sync | Authenticated | Get history |
| `/api/orchestrator/chat/{id}/` | DELETE | Sync | Authenticated | Delete history |
| `/api/orchestrator/chat/context-aware/` | POST | **Async** | Authenticated | Context-aware chat |

### AI Workflow Generation ✨
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/orchestrator/ai/generate/` | POST | **Async** | Authenticated | Generate from description |
| `/api/orchestrator/workflows/{id}/ai/modify/` | POST | **Async** | Owner | Modify with NL |
| `/api/orchestrator/workflows/{id}/ai/suggest/` | GET | **Async** | Owner | Get AI suggestions |

### Partial Execution ✨
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/orchestrator/workflows/{id}/partial-execute/` | POST | **Async** | Owner | Test single node |

### Thought History ✨
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/orchestrator/executions/{id}/thoughts/` | GET | Sync | Owner | Get execution thoughts |

---

## 📊 Logs & Analytics APIs (`/api/logs/`)

### Insights
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/logs/insights/stats/` | GET | **Async** | Authenticated | Execution statistics |
| `/api/logs/insights/workflow/{id}/` | GET | **Async** | Owner | Per-workflow metrics |
| `/api/logs/insights/costs/` | GET | **Async** | Authenticated | Cost breakdown |

### Audit Trail
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/logs/audit/` | GET | **Async** | Authenticated | List own audit entries |
| `/api/logs/audit/export/` | GET | **Async** | Authenticated | Export CSV/JSON |

### Execution History
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/logs/executions/` | GET | **Async** | Authenticated | List executions |
| `/api/logs/executions/{id}/` | GET | **Async** | Owner | Execution details |

---

## 🧠 Inference APIs (`/api/inference/`)

### Documents
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/inference/documents/` | GET | **Async** | Authenticated | List documents |
| `/api/inference/documents/` | POST | **Async** | Authenticated | Upload document |
| `/api/inference/documents/{id}/` | GET | **Async** | Owner | View document |
| `/api/inference/documents/{id}/` | DELETE | **Async** | Owner | Delete document |
| `/api/inference/documents/{id}/share/` | POST | **Async** | Owner | Toggle platform share |
| `/api/inference/documents/{id}/download/` | GET | **Async** | Owner | Download content |

### RAG
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/inference/rag/search/` | POST | **Async** | Authenticated | Search documents |
| `/api/inference/rag/query/` | POST | **Async** | Authenticated | RAG Q&A |

---

## 🧩 Node APIs (`/api/nodes/`)

### Node Registry
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/nodes/` | GET | Sync | Authenticated | List all nodes |
| `/api/nodes/categories/` | GET | Sync | Authenticated | Nodes by category |
| `/api/nodes/{type}/schema/` | GET | Sync | Authenticated | Get node schema |

### Custom Nodes
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/nodes/custom/` | GET | Sync | Authenticated | List custom nodes |
| `/api/nodes/custom/` | POST | Sync | Authenticated | Create custom node |
| `/api/nodes/custom/{id}/` | GET | Sync | Owner | View custom node |
| `/api/nodes/custom/{id}/` | DELETE | Sync | Owner | Delete custom node |

---

## ⚙️ Compiler APIs (`/api/compile/`)

| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/compile/` | POST | **Async** | Authenticated | Compile workflow |
| `/api/compile/validate/` | POST | **Async** | Authenticated | Validate only |
| `/api/workflows/{id}/compile/` | POST | **Async** | Owner | Compile specific workflow |
| `/api/workflows/{id}/validate/` | POST | **Async** | Owner | Validate specific workflow |

---

## 🔐 Credentials APIs (`/api/credentials/`)

| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/credentials/` | GET | Sync | Authenticated | List own credentials |
| `/api/credentials/` | POST | Sync | Authenticated | Create credential |
| `/api/credentials/{id}/` | GET | Sync | Owner | View metadata |
| `/api/credentials/{id}/` | PUT | Sync | Owner | Update credential |
| `/api/credentials/{id}/` | DELETE | Sync | Owner | Delete credential |
| `/api/credentials/{id}/verify/` | POST | **Async** | Owner | Verify credential |
| `/api/credentials/types/` | GET | Sync | Authenticated | List types |
| `/api/credentials/oauth/google/init/` | GET | Sync | Authenticated | Init Google OAuth |
| `/api/credentials/oauth/google/callback/` | POST | **Async** | Authenticated | Google OAuth callback |
| `/api/credentials/audit/` | GET | Sync | Authenticated | Credential audit logs |

> ⚠️ **Security**: Credential values are NEVER returned. Only metadata visible.

---

## 📡 Streaming APIs (`/api/streaming/`)

### SSE Endpoints
| Endpoint | Method | Type | Permission | Notes |
|----------|--------|------|------------|-------|
| `/api/streaming/executions/{id}/stream/` | GET | Sync/SSE | Owner | SSE execution events |

### WebSocket Endpoints
| Endpoint | Type | Permission | Notes |
|----------|------|------------|-------|
| `/ws/execution/{execution_id}/` | **Async** | Owner | Real-time updates & HITL |
| `/ws/hitl/` | **Async** | Authenticated | Global HITL notifications |

---

## 🔒 Rate Limits by Tier

| Endpoint Category | Free | Pro | Enterprise |
|-------------------|------|-----|------------|
| Auth endpoints | 10/min | 50/min | 200/min |
| Workflow CRUD | 20/min | 100/min | 500/min |
| Compile | 10/min | 100/min | Unlimited |
| Execute | 5/min | 50/min | 200/min |
| AI Generation | 10/day | 100/day | Unlimited |
| Document upload | 10/day | 100/day | Unlimited |
| AI Chat messages | 50/day | 500/day | Unlimited |
| Stream connections | 5 | 20 | 100 |

---

## 🛡️ Security Headers

All responses include:
```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
X-XSS-Protection: 1; mode=block
Referrer-Policy: strict-origin-when-cross-origin
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'
Permissions-Policy: geolocation=(), microphone=(), camera=()
```

Rate limit headers:
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1611234567
```

---

## 📝 Permission Classes

```python
# core/permissions.py

class IsOwner(BasePermission):
    """Object-level: user must own resource"""
    
class IsOwnerOrAdmin(BasePermission):
    """Owner OR staff/admin"""
    
class IsOwnerOrReadOnly(BasePermission):
    """Owner can modify, others read-only"""
    
class HasAPIKey(BasePermission):
    """Valid X-API-Key header"""
    
class IsProTier(TierPermission):
    """Pro tier or higher"""
    
class IsEnterpriseTier(TierPermission):
    """Enterprise tier only"""
    
class HasCredits(BasePermission):
    """User has remaining credits"""
```

---

## 📝 Summary Table

| Category | Sync/Async | Base Path | Endpoints | Default Permission |
|----------|------------|-----------|-----------|--------------------|
| **Core** | Sync | `/api/` | 10 | Authenticated |
| **Orchestrator** | Mixed | `/api/orchestrator/` | 24 | Owner |
| **Inference** | **Async** | `/api/inference/` | 8 | Owner |
| **Compiler** | **Async** | `/api/compile/` | 4 | Authenticated |
| **Credentials** | Mixed | `/api/credentials/` | 10 | Owner |
| **Nodes** | Sync | `/api/nodes/` | 3 | Authenticated |
| **Logs** | **Async** | `/api/logs/` | 7 | Authenticated |
| **Streaming** | Mixed | `/api/streaming/` | 5 | Owner |

---
*Updated: 2026-02-05*
