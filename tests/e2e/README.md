# Backend End-to-End Tests

These run against a **live, running backend** — not a Django test DB.
They make real HTTP / WebSocket calls and assume migrations are applied.

## Prereqs

```bash
# Terminal 1 — start the server
cd Backend
python manage.py migrate
python manage.py runserver 0.0.0.0:8000

# Terminal 2 — run e2e
cd Backend
python tests/e2e/run_smoke.py            # full smoke
python tests/e2e/run_smoke.py --base http://staging.example.com
python tests/e2e/test_websocket.py       # streaming smoke
python tests/e2e/run_chaos.py            # adversarial / load probe
```

Override the target with `BASE_URL=...` env var or `--base`.

| Script | What it does |
|--------|--------------|
| `run_smoke.py` | Register → login → create workflow → execute → poll for completion |
| `test_websocket.py` | Connect to streaming WS, validate at least one event arrives |
| `run_chaos.py` | Sad/angry probes: malformed bodies, IDOR scans, slow-loris, oversized payloads |

These are **scripts**, not pytest. They print PASS/FAIL and exit with a
non-zero code on any failure so they slot into CI. They are intentionally
self-contained (only `requests` + `websockets` from the project venv).
