# `datasources/`: your own databases and APIs as tools

Lets the AI query **your** database or call **your** API through one generic
tool each (`query_sql` in `chat/tools/data.py`, `call_api` in
`chat/tools/apicaller.py`), instead of one connector per system. The web app
calls these "Custom tools".

## Data (`models.py`)

| Model | What it is |
|---|---|
| `DataConnection` | A database (SQLite file or PostgreSQL). The password is a secret reference, not stored here |
| `ApiConnection` | An HTTP API: base URL, auth, operations |
| `SharedTool` | A published copy others can install (without credentials) |

## Files

| File | What it does |
|---|---|
| `drivers.py` | Running SQL against each database kind, with host checks and a row cap |
| `sqlcheck.py` | Deciding which SQL is allowed by *parsing* it, not by pattern matching |
| `sharing.py` | Publishing and installing shared tools |
| `views.py`, `serializers.py`, `urls.py` | `/api/datasources/...` |

Tests: `datasources/tests/`.
