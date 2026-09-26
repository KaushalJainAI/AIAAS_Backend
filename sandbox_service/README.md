# `sandbox_service/`: the container that runs the AI's Python

The AI writes Python code, and we have to run it without trusting it. This
folder is a small, separate program that does that inside a locked-down
container. It serves the `execute_python` and `run_python_on_files` tools.

It is **not** part of the Django app, and the backend never imports it. The
backend sends code to it over HTTP, on a private network that only the two of
them share. The backend side of that conversation is `../sandbox/`.

Full design and threat model: [`docs/SANDBOX_EXECUTION.md`](../docs/SANDBOX_EXECUTION.md).

## Files

| File | What it does |
|---|---|
| `server.py` | A tiny HTTP server with two routes: `GET /health` and `POST /execute`. It runs only a few jobs at once |
| `executor.py` | Starts a fresh, restricted process for each run: memory and CPU limits, a throwaway working folder, and the whole process group killed on timeout |
| `runner.py` | The code that runs inside that process. It blocks network sockets where it can, runs the snippet, and prints one JSON result |
| `Dockerfile` | The container image: Python 3.12 slim, numpy and pandas, running as a non-root user |
| `requirements.txt` | Kept very small on purpose, because this process runs untrusted code |

## What a run sends and gets back

Send `{"code": "..."}`. To give the code files to read, add
`"files": {"name": "<base64>"}`. To get files back, add `"collect": ["name"]`.

Every run answers in the same shape. The backend's in-process fallback (for
local development) answers in this shape too:

```json
{"success": true, "result": 42, "output": "stdout…", "stderr": "", "error": null, "timed_out": false}
```

When files were collected, the answer also has `files_out`.

## Running it

Normally it starts with everything else:

```bash
docker compose up --build                                   # local
docker compose -f docker-compose.prod.yml pull sandbox \
  && docker compose -f docker-compose.prod.yml up -d        # production (the image is built and pushed from a dev machine)
```

For a quick check without Docker (uses your own Python, so numpy only works if
you have it installed):

```bash
cd Backend/sandbox_service && python server.py
curl -s -XPOST localhost:8100/execute -H 'Content-Type: application/json' -d '{"code":"result=2+2"}'
```

## Tests

`tests/`: `test_executor.py` (runs real processes), `test_files.py` (files in
and out), `test_concurrency.py`. Run them with `python -m pytest sandbox_service/tests`
from `Backend/`.

## What keeps a run contained

Three layers, from strongest to weakest:

1. **The container.** It has no route to the internet, drops all Linux
   privileges, has a read-only file system, runs as a non-root user, and has
   memory and process-count limits. **This is the real wall.** Even code that
   fully escapes Python lands in a throwaway box with no secrets in it.
2. **The process.** Each run gets its own process with resource limits, and on
   timeout the whole process group is killed, not just asked to stop.
3. **Seccomp.** A kernel filter blocks opening network sockets, where the
   system supports it.

Layers 2 and 3 are extra depth. If they fail, layer 1 still holds.
