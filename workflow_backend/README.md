# `workflow_backend/`: project settings and shared plumbing

The Django *project* (not an app). The name comes from the old workflow product.

## Settings (`settings/`)

| File | Used by |
|---|---|
| `base.py` | Everything shared. **Add new settings here** |
| `local.py` | `manage.py` and local dev: SQLite, no Redis needed |
| `test.py` | Tests: in-memory, no outside services |
| `deployment.py` | The production Docker image: Redis, security headers |

Each entry point picks its settings file explicitly. There is no top-level
`settings.py`.

## Files

| File | What it does |
|---|---|
| `urls.py` | The root URL table: mounts every app under `/api/...` |
| `asgi.py` | The server entry point (HTTP and WebSockets) |
| `wsgi.py`, `celery.py` | Other entry points |
| `background.py` | `spawn()`: **the way to start work that outlives a request.** Never use bare `asyncio.create_task` for that |
| `httpclient.py` | `shared_client()`: one reused HTTP client for outgoing calls. Don't wrap it in `async with`; that closes it for everyone |
| `thresholds.py` | Limits and numbers used across the app (list caps, prices...) |
| `observability.py` | Error reporting (Sentry). Off unless `SENTRY_DSN` is set |
| `loopwatch.py` | Measures how often the server gets stuck |
