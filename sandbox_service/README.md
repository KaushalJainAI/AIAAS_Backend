# sandbox_service — the code-execution sidecar

A standalone, hardened container that runs untrusted, LLM-authored Python for
the `execute_python` tool. It is **not** part of the Django app and the backend
never imports it — the backend talks to it over HTTP on an internal network.

See `Backend/docs/SANDBOX_EXECUTION.md` for the full design and threat model.

## Files

| File | Role |
|------|------|
| `server.py` | Stdlib HTTP server. `GET /health`, `POST /execute`. Concurrency-capped. |
| `executor.py` | Spawns one locked-down subprocess per run (rlimits, own session, killpg on timeout, ephemeral cwd). Importable/testable. |
| `runner.py` | The in-child harness: applies best-effort seccomp, runs the snippet, emits one JSON envelope. |
| `Dockerfile` | `python:3.12-slim` + numpy/pandas + pyseccomp, non-root, healthcheck. |
| `requirements.txt` | Deliberately tiny — this process runs untrusted code. |

## The envelope

Both this service and the backend's in-process fallback return the same shape:

```json
{"success": true, "result": 42, "output": "stdout…", "stderr": "", "error": null, "timed_out": false}
```

## Running it

Via compose (normal path):

```bash
docker compose up --build            # local
docker compose -f docker-compose.ec2.yml build sandbox && docker compose -f docker-compose.ec2.yml up   # ec2
```

Directly, for a quick check (no Docker; uses the host Python, so no numpy unless
installed locally):

```bash
cd Backend/sandbox_service && python server.py
curl -s -XPOST localhost:8100/execute -H 'Content-Type: application/json' -d '{"code":"result=2+2"}'
```

## Tests

```bash
python Backend/sandbox_service/tests/test_executor.py      # runs the real subprocess
# or via pytest, which collects it by filename
```

## What confines a run

Container (network-none-by-being-internal, `cap_drop: ALL`, read-only root,
non-root, mem/pids caps) → subprocess (`setrlimit`, own session, killpg) →
seccomp (blocks sockets). The container is the real boundary; the rest is depth.
