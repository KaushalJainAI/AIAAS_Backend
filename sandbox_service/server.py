"""The sandbox sidecar's HTTP surface.

Stdlib only, on purpose: this process runs untrusted code, so the smaller its
own dependency surface the better. It exposes exactly two routes on an
internal-only network (no ports are published to the host):

    GET  /health        -> {"status": "ok"}
    POST /execute       -> {code, wall_seconds?, cpu_seconds?, mem_mb?,
                            files?: {name: base64}, collect?: [name]} -> envelope
                            (+ files_out: {name: base64}, missing, unsaved)

The backend is the only client; there is no auth here because the network it
sits on has no route in from anywhere else (see compose: an `internal: true`
network, no published ports). A concurrency cap keeps a burst of requests from
spawning more subprocesses than a small box can hold.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from executor import MAX_FILE_BYTES, MAX_FILES, FileSpecError, execute

# Ceilings the caller cannot exceed, whatever it asks for. The backend passes
# its own (smaller) defaults; these stop a caller from requesting a run that
# would threaten the box.
MAX_WALL_SECONDS = int(os.environ.get("SANDBOX_MAX_WALL_SECONDS", "30"))
MAX_CPU_SECONDS = int(os.environ.get("SANDBOX_MAX_CPU_SECONDS", "25"))
MAX_MEM_MB = int(os.environ.get("SANDBOX_MAX_MEM_MB", "512"))
MAX_CODE_BYTES = int(os.environ.get("SANDBOX_MAX_CODE_BYTES", str(256 * 1024)))
MAX_CONCURRENCY = int(os.environ.get("SANDBOX_MAX_CONCURRENCY", "2"))

_slots = threading.Semaphore(MAX_CONCURRENCY)

#: Body ceiling: the code, plus every input file base64-encoded (4/3), plus JSON.
MAX_BODY_BYTES = MAX_CODE_BYTES + (MAX_FILES * MAX_FILE_BYTES * 4) // 3 + 64 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/execute":
            self._send(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(413, {"error": "request too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send(400, {"error": "invalid JSON body"})
            return

        code = payload.get("code")
        if not isinstance(code, str) or not code.strip():
            self._send(400, {"error": "'code' is required"})
            return
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            self._send(413, {"error": "code too large"})
            return

        wall = min(int(payload.get("wall_seconds", 10)), MAX_WALL_SECONDS)
        cpu = min(int(payload.get("cpu_seconds", 8)), MAX_CPU_SECONDS)
        mem_mb = min(int(payload.get("mem_mb", 384)), MAX_MEM_MB)

        try:
            files = {
                name: base64.b64decode(data, validate=True)
                for name, data in (payload.get("files") or {}).items()
            }
        except (binascii.Error, TypeError, ValueError, AttributeError):
            self._send(400, {"error": "files must map names to base64"})
            return
        collect = payload.get("collect") or []
        if not isinstance(collect, list):
            self._send(400, {"error": "collect must be a list of names"})
            return

        acquired = _slots.acquire(timeout=MAX_WALL_SECONDS)
        if not acquired:
            self._send(503, {"error": "sandbox busy, try again"})
            return
        try:
            result = execute(
                code,
                wall_seconds=wall,
                cpu_seconds=cpu,
                mem_bytes=mem_mb * 1024 * 1024,
                files=files,
                collect=collect,
            )
        except FileSpecError as exc:
            self._send(400, {"error": str(exc)})
            return
        finally:
            _slots.release()

        if "files_out" in result:
            result["files_out"] = {
                name: base64.b64encode(data).decode("ascii")
                for name, data in result["files_out"].items()
            }

        self._send(200, result)

    def log_message(self, *args) -> None:  # keep the untrusted-run log quiet
        pass


def main() -> None:
    host = os.environ.get("SANDBOX_HOST", "0.0.0.0")
    port = int(os.environ.get("SANDBOX_PORT", "8100"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[sandbox] listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
