# `streaming/`: live updates over WebSockets

Plumbing that pushes live events to the browser.

## WebSockets (`routing.py`)

| Path | Consumer | Used for |
|---|---|---|
| `ws/execution/<id>/` | `ExecutionConsumer` | Live steps of one agent run (Activity page, via `src/hooks/useLiveRun.ts`) |
| `ws/hitl/` | `HITLNotificationConsumer` | Per-user events: approval reminders and new notifications (`src/hooks/useHITLReminders.ts`) |
| `ws/imagine-agent/` | `imagine.consumers.ImagineAgentConsumer` | The Studio's conversational agent |

## Files

| File | What it does |
|---|---|
| `routing.py` | The WebSocket URL table, loaded by `workflow_backend/asgi.py` |
| `consumers.py` | The two consumers above |
| `broadcaster.py` | Sends run events to the right channel group. `agents/agent/stream.py` calls it |
| `views.py`, `urls.py` | Server-sent-event endpoints under `/api/streaming/`. **The web app does not call these**; it uses the WebSocket |
| `models.py` | `StreamEvent` |

Chat does **not** use this app. It streams over the HTTP response itself
(`chat/transport/`).

In production, WebSockets need Redis (`REDIS_URL`) so events reach every
server process. Locally an in-memory layer is used.
