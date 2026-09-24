# `core/`: users, login, and the request pipeline

Everything about **who is calling** and the plumbing every request passes
through.

## Data (`models.py`)

| Model | What it is |
|---|---|
| `UserProfile` | Per-user settings and account state: tier, credits, default model and effort, default autonomy, `paused_until` ("pause everything") |
| `UserMemory` | Facts the assistant remembers about you across chats (`memory.py`). Viewed and deleted in Settings → Memory, or with `/memory` in chat |
| `APIKey` | Keys for calling the API without a browser login |
| `UsageTracking` | Usage counters |
| `PasswordOTP` | One-time codes for password reset |

## Files

| File | What it does |
|---|---|
| `views.py`, `urls.py`, `serializers.py` | Sign up, log in, profile, API keys, memory, usage (`/api/auth/...`, `/api/memory/`, `/api/usage/`) |
| `memory.py` | Reading and writing `UserMemory`. Keeps it small: repeats are merged, old facts are dropped per category |
| `preferences.py` | Your settings, read once per turn and written into the prompt |
| `auth/` | How a request is identified: JWT, API key, or a token in the URL (for downloads); and permission classes |
| `http/middleware.py` | Custom middleware (runs in both sync and async mode) |
| `http/throttling.py` | Rate limits by account tier |
| `http/pagination.py` | Page sizes for list endpoints |
| `realtime/` | WebSocket login (`channels_middleware.py`) and a base class for per-user sockets |
| `safety/net.py` | Checks outgoing URLs: blocks internal addresses (SSRF), applies per-agent allow-lists |
| `safety/security.py` | Cleaning untrusted input |

Google / social login uses `django-allauth`.

## Tests

`core/tests/`: `test_memory.py`, `test_net.py`, `test_hybrid_middleware.py`.
