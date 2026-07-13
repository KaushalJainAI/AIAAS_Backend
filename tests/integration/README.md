# Backend Integration Tests

Cross-app tests that wire several Django apps together against an in-memory
SQLite DB. They use Django's `TestCase` (so each test runs inside a
transaction that gets rolled back) and the test settings module, which means
**no Redis, no PostgreSQL, no external network**.

## Run

```bash
# Whole integration suite
python manage.py test tests.integration --settings=workflow_backend.settings.test --verbosity=2

# Single module
python manage.py test tests.integration.test_auth_flow --settings=workflow_backend.settings.test
```

Or with pytest:
```bash
pytest tests/integration -v
```

## What's covered

| File | Apps exercised | Adversarial focus |
|------|----------------|-------------------|
| `test_auth_flow.py` | `core`, `dj_rest_auth` | Credential reuse, throttling, token tampering |
| `test_workflow_lifecycle.py` | `orchestrator`, `compiler`, `nodes` | Cyclic graphs, oversized payloads, unauthorized access |
| `test_credentials_mcp.py` | `credentials`, `mcp_integration` | Cross-user credential leak, decryption failures, mapping injection |
| `test_adversarial_credentials.py` | `credentials` | Encryption tampering, wrong key, malformed input |
| `test_adversarial_compiler.py` | `compiler` | Cycles, dangling edges, malformed nodes, deep nesting |
| `test_adversarial_orchestrator.py` | `orchestrator` | IDOR, mass assignment, oversized state, race conditions |

## Conventions

- Every adversarial file has three classes: `HappyPath`, `SadPath`, `AngryPath`.
  - **Happy** — the documented golden path works.
  - **Sad** — predictable failures (missing field, wrong type, empty string).
  - **Angry** — hostile input designed to break invariants
    (huge strings, recursion bombs, unicode tricks, IDOR, race conditions).
- No mocking of DB layers — use real models against in-memory SQLite.
- Mock external services (HTTP, MCP subprocess, LLM SDK) only at their boundary.
