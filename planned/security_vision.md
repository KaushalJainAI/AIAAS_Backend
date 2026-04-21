# Security Hardening Post-Mortem

> 🚨 **Critical Issues Identified from Agentic-AI Backend Analysis**
> *This document preserves the historical audit and fixes applied to the old Flask implementation when migrating to Django.*

The following security loopholes were discovered in the existing Agentic-AI implementation (`host.py`, `host.py`, `langgraph_super_agent.py`, `connections.py`). These MUST be addressed in the Django backend:

## 🔴 Critical Security Fixes

### 1. Authentication & Authorization
**Current Issue**: Flask endpoints have no auth - anyone can call `/chat`, `/clearMem`.

**Fix Required**:
```python
# core/authentication.py
class JWTAuthentication:
    """JWT-based authentication for API requests"""
    pass

class APIKeyAuthentication:
    """API key authentication for programmatic access"""
    pass

# Every endpoint must have:
@permission_classes([IsAuthenticated])
```

**Checklist**:
- [x] JWT token generation/validation
- [x] API key per user with rotation support
- [x] Permission classes per endpoint
- [x] Admin-only routes protection

---

### 2. Rate Limiting
**Current Issue**: No rate limiting = DoS vulnerability, cost explosion from LLM calls.

**Fix Required**:
```python
# core/middleware.py
from django_ratelimit.decorators import ratelimit

# Apply per-endpoint limits:
# - /compile: 10/minute  
# - /execute: 5/minute
# - /stream: 20 connections
```

**Tier-based limits**:
| Tier | Compile | Execute | Stream |
|------|---------|---------|--------|
| Free | 10/min | 5/min | 5 |
| Pro | 100/min | 50/min | 20 |
| Enterprise | Unlimited | 200/min | 100 |

---

### 3. Input Sanitization (Prompt Injection)
**Current Issue**: User input passed directly to LLM without sanitization.

**Fix Required**:
```python
# core/security.py
class InputSanitizer:
    """
    Sanitize user inputs before LLM processing:
    - Strip prompt injection patterns
    - Limit input length
    - Escape special characters
    - Block known malicious patterns
    """
    
    BLOCKED_PATTERNS = [
        r"ignore previous instructions",
        r"system prompt",
        r"</?(system|user|assistant)>",
    ]
```

---

### 4. Request Timeouts
**Current Issue**: No timeout on agent execution - can hang indefinitely.

**Fix Required**:
```python
# executor/runner.py
import asyncio

async def execute_with_timeout(workflow, timeout=300):
    try:
        result = await asyncio.wait_for(
            execute_workflow(workflow),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.error("Workflow execution timed out")
        return {"error": "Execution timeout", "success": False}
```

**Timeouts**:
- Workflow execution: 5 minutes (configurable)
- Individual node: 60 seconds
- HTTP requests: 30 seconds

---

### 5. Secrets Management
**Current Issue**: API keys in `.env` shared across all agents, logs may contain sensitive data.

**Fix Required**:
- [x] Per-user credential isolation (already in checklist)
- [x] Encryption at rest for credentials (AES-256)
- [x] Audit logging for credential access
- [x] Log sanitization (strip PII, secrets before logging)

```python
# core/logging.py
class SanitizedLogger:
    SENSITIVE_PATTERNS = [
        r"api_key[\"']?\s*[:=]\s*[\"'][^\"']+[\"']",
        r"password[\"']?\s*[:=]\s*[\"'][^\"']+[\"']",
        r"Bearer\s+[A-Za-z0-9\-._~+/]+=*",
    ]
    
    def sanitize(self, message):
        for pattern in self.SENSITIVE_PATTERNS:
            message = re.sub(pattern, "[REDACTED]", message)
        return message
```

---

### 6. CORS Configuration
**Current Issue**: No CORS headers configured.

**Fix Required**:
```python
# settings.py
CORS_ALLOWED_ORIGINS = [
    "https://yourfrontend.com",
]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
```

---

### 7. Thread Safety
**Current Issue**: Shared orchestrator singleton across threads without locking.

**Fix Required**: 
- Use async execution context per-request
- Implement proper state isolation
- Use thread-local storage for user context

---

## 🟠 Architecture Improvements

### 8. Human-in-the-Loop Implementation
**Current Issue**: README mentions approval gates but they're not implemented.

**Fix Required**:
```python
# orchestrator/approval.py
class ApprovalGate:
    """
    Block execution for sensitive operations:
    - Database modifications
    - File operations
    - External API calls (non-whitelisted)
    - Credential access
    """
    
    APPROVAL_REQUIRED = [
        "database_write",
        "file_delete",
        "external_api",
        "credential_access",
    ]
    
    async def request_approval(self, user_id, operation, details):
        # Send notification to user
        # Block until approved or timeout
        pass
```

---

### 9. Message Queue for Scaling
**Current Issue**: In-memory queue loses messages on restart, max 5 messages.

**Fix Required**:
- Use Redis/Celery for task queue
- Persistent message storage
- Horizontal scaling support

---

### 10. Secure Agent Method Execution
**Current Issue**: Dynamic `getattr` allows LLM to potentially call any method.

**Fix Required**:
```python
# executor/safe_executor.py
ALLOWED_METHODS = {
    "Chatbot": ["chat"],
    "WebSearchingAgent": ["run", "search"],
    "DatabaseOrchestrator": ["query"],
}

def safe_execute(agent_name, method_name, *args, **kwargs):
    if method_name not in ALLOWED_METHODS.get(agent_name, []):
        raise SecurityError(f"Method {method_name} not allowed on {agent_name}")
    # proceed with execution
```

---

## 📊 Security Summary

| Priority | Issue | Status | Effort |
|----------|-------|--------|--------|
| 🔴 Critical | No Authentication | ✅ Done | 4h |
| 🔴 Critical | No Rate Limiting | ✅ Done | 2h |
| 🔴 Critical | Prompt Injection | ✅ Done | 3h |
| 🔴 Critical | No Timeouts | ✅ Done | 1h |
| 🟠 High | Secrets in Logs | ✅ Done | 2h |
| 🟠 High | CORS Config | ✅ Done | 0.5h |
| 🟠 High | Thread Safety | ✅ Done | 3h |
| 🟠 High | Approval Gates | ✅ Done | 4h |
| 🟡 Medium | Message Queue | ✅ Done | 4h |
| 🟡 Medium | Safe Method Exec | ✅ Done | 2h |

**Total Security Hardening: ~25.5 hours (COMPLETED)**
