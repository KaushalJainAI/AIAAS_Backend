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
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/auth/register` | POST | Public | Rate limit: 5/hour |
| `/api/auth/login` | POST | Public | Rate limit: 10/min |
| `/api/auth/refresh` | POST | Authenticated | Refresh JWT token |
| `/api/auth/logout` | POST | Authenticated | Invalidate token |

### User Profile
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/users/me` | GET | Authenticated | Own profile only |
| `/api/users/me` | PATCH | Authenticated | Own profile only |
| `/api/users/{id}` | GET | Admin | View any user |
| `/api/users/` | GET | Admin | List all users |

### API Keys
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/keys/` | GET | Authenticated | List own keys |
| `/api/keys/` | POST | Authenticated | Create new key |
| `/api/keys/{id}` | DELETE | Owner | Delete own key |
| `/api/keys/{id}/rotate` | POST | Owner | Rotate key |

---

## 🤖 Orchestrator APIs (`/api/orchestrator/`)

### Workflows CRUD
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/orchestrator/workflows/` | GET | Authenticated | List own workflows |
| `/api/orchestrator/workflows/` | POST | Authenticated | Create workflow |
| `/api/orchestrator/workflows/{id}/` | GET | Owner | View workflow |
| `/api/orchestrator/workflows/{id}/` | PUT | Owner | Update workflow |
| `/api/orchestrator/workflows/{id}/` | DELETE | Owner | Delete workflow |

### Version History
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/orchestrator/workflows/{id}/versions/` | GET | Owner | List versions |
| `/api/orchestrator/workflows/{id}/versions/` | POST | Owner | Create snapshot |
| `/api/orchestrator/workflows/{id}/versions/{v}/restore/` | POST | Owner | Restore version |

### Execution Control
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/orchestrator/workflows/{id}/execute/` | POST | Owner | Start execution |
| `/api/orchestrator/executions/{id}/status/` | GET | Owner | Get status |
| `/api/orchestrator/executions/{id}/pause/` | POST | Owner | Pause execution |
| `/api/orchestrator/executions/{id}/resume/` | POST | Owner | Resume execution |
| `/api/orchestrator/executions/{id}/stop/` | POST | Owner | Stop execution |

### HITL (Human-in-the-Loop)
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/orchestrator/hitl/pending/` | GET | Authenticated | Pending requests |
| `/api/orchestrator/hitl/{id}/respond/` | POST | Owner | Submit response |

### AI Chat
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/orchestrator/chat/` | GET | Authenticated | List conversations |
| `/api/orchestrator/chat/` | POST | Authenticated | Send message |
| `/api/orchestrator/chat/{id}/` | GET | Authenticated | Get history |
| `/api/orchestrator/chat/context-aware/` | POST | Authenticated | Context-aware chat |

### AI Workflow Generation ✨ NEW
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/orchestrator/ai/generate/` | POST | Authenticated | Generate from description |
| `/api/orchestrator/workflows/{id}/ai/modify/` | POST | Owner | Modify with NL |
| `/api/orchestrator/workflows/{id}/ai/suggest/` | GET | Owner | Get AI suggestions |

### Thought History ✨ NEW
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/orchestrator/executions/{id}/thoughts/` | GET | Owner | Get execution thoughts |

---

## 📊 Logs & Analytics APIs (`/api/logs/`)

### Insights
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/logs/insights/stats/` | GET | Authenticated | Execution statistics |
| `/api/logs/insights/workflow/{id}/` | GET | Owner | Per-workflow metrics |
| `/api/logs/insights/costs/` | GET | Authenticated | Cost breakdown |

### Audit Trail
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/logs/audit/` | GET | Authenticated | List own audit entries |
| `/api/logs/audit/export/` | GET | Authenticated | Export CSV/JSON |

### Execution History
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/logs/executions/` | GET | Authenticated | List executions |
| `/api/logs/executions/{id}/` | GET | Owner | Execution details |

---

## 🧠 Inference APIs (`/api/inference/`)

### Documents
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/inference/documents/` | GET | Authenticated | List documents |
| `/api/inference/documents/` | POST | Authenticated | Upload document |
| `/api/inference/documents/{id}/` | GET | Owner | View document |
| `/api/inference/documents/{id}/` | DELETE | Owner | Delete document |

### RAG
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/inference/rag/search/` | POST | Authenticated | Search documents |
| `/api/inference/rag/query/` | POST | Authenticated | RAG Q&A |

---

## 🧩 Node APIs (`/api/nodes/`)

### Node Registry
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/nodes/` | GET | Authenticated | List all nodes |
| `/api/nodes/{type}/schema/` | GET | Authenticated | Get node schema |

### Custom Nodes
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/nodes/custom/` | GET | Authenticated | List custom nodes |
| `/api/nodes/custom/` | POST | Authenticated | Create custom node |
| `/api/nodes/custom/{id}/` | GET | Owner | View custom node |
| `/api/nodes/custom/{id}/` | DELETE | Owner | Delete custom node |

---

## ⚙️ Compiler APIs (`/api/compile/`)

| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/compile/` | POST | Authenticated | Compile workflow |
| `/api/compile/validate/` | POST | Authenticated | Validate only |

---

## 🔐 Credentials APIs (`/api/credentials/`)

| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/credentials/` | GET | Authenticated | List own credentials |
| `/api/credentials/` | POST | Authenticated | Create credential |
| `/api/credentials/{id}/` | GET | Owner | View metadata |
| `/api/credentials/{id}/` | PUT | Owner | Update credential |
| `/api/credentials/{id}/` | DELETE | Owner | Delete credential |
| `/api/credentials/types/` | GET | Authenticated | List types |

> ⚠️ **Security**: Credential values are NEVER returned. Only metadata visible.

---

## 📡 Streaming APIs (`/api/streaming/`)

### SSE Endpoints
| Endpoint | Method | Permission | Notes |
|----------|--------|------------|-------|
| `/api/streaming/execution/{id}/` | GET | Owner | SSE execution events |

### WebSocket Endpoints
| Endpoint | Permission | Notes |
|----------|------------|-------|
| `/ws/execution/{id}/` | Owner | Real-time updates |
| `/ws/hitl/` | Authenticated | HITL notifications |

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

## Summary

| Category | Endpoints | Auth Required | Owner Required | Admin Only |
|----------|-----------|---------------|----------------|------------|
| Core | 12 | 10 | 4 | 2 |
| Orchestrator | 22 | 22 | 16 | 0 |
| Logs | 7 | 7 | 2 | 0 |
| Inference | 6 | 6 | 2 | 0 |
| Nodes | 6 | 6 | 3 | 0 |
| Compiler | 2 | 2 | 0 | 0 |
| Credentials | 6 | 6 | 4 | 0 |
| Streaming | 3 | 3 | 2 | 0 |
| **Total** | **64** | **62** | **33** | **2** |
