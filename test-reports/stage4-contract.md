# Stage 4 — Endpoint contract audit

Validated against live OpenAPI schema (drf-spectacular) using jsonschema Draft7.

| Method | Path | Status | Match | Notes |
|---|---|---|---|---|
| GET | `/api/health/` | 200 | SKIP | endpoint not in schema (undocumented) |
| GET | `/api/auth/profile/` | 200 | OK | matches schema |
| GET | `/api/orchestrator/workflows/` | 200 | OK | matches schema |
| POST | `/api/orchestrator/workflows/` | 201 | OK | matches schema |
| GET | `/api/orchestrator/workflows/93/` | 200 | OK | matches schema |
| PATCH | `/api/orchestrator/workflows/93/` | 200 | OK | matches schema |
| DELETE | `/api/orchestrator/workflows/93/` | 204 | OK | documented 204 no content |
| GET | `/api/credentials/` | 200 | FAIL | 1 error(s); first at <root>: {'credentials': []} is not of type 'array' |
| GET | `/api/credentials/types/` | 200 | FAIL | 1 error(s); first at <root>: {'types': [{'id': 14, 'name': 'Airtable API', 'slug': 'airtable', 'service_identifier': None, 'description': 'Airtable P |
| GET | `/api/logs/audit/` | 200 | OK | matches schema |
| GET | `/api/logs/executions/` | 200 | OK | matches schema |
| GET | `/api/logs/insights/stats/` | 200 | OK | matches schema |
| GET | `/api/mcp/servers/` | 200 | FAIL | 1 error(s); first at <root>: {'servers': [{'id': 21, 'name': 'Fetch', 'type': 'stdio', 'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server |
| GET | `/api/skills/` | 200 | OK | matches schema |
| GET | `/api/inference/kbs/` | 200 | OK | matches schema |
| GET | `/api/inference/documents/` | 200 | OK | matches schema |
| GET | `/api/notifications/` | 200 | OK | matches schema |
| GET | `/api/orchestrator/chat/` | 200 | OK | matches schema |
| GET | `/api/orchestrator/templates/` | 200 | OK | matches schema |
| POST | `/api/orchestrator/workflows/94/execute/` | 200 | OK | matches schema |

**Totals:** 16 OK / 3 FAIL / 1 SKIP