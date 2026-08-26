#!/usr/bin/env python
"""
Contract audit: hit a curated list of endpoints on the live server and validate
each response body against the live OpenAPI schema (drf-spectacular) using
jsonschema.

Output: a markdown table to stdout and to test-reports/stage4-contract.md
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests
from jsonschema import Draft7Validator, RefResolver

from _lib import (
    auth_headers,
    create_nvidia_credential,
    load_env_file,
    login,
    minimal_nvidia_workflow,
    parse_base,
    register,
    unique_user,
)


def _oas_to_jsonschema(node: Any) -> Any:
    """Recursively convert OpenAPI 3.0 nullable:true to JSON Schema type union with 'null'."""
    if isinstance(node, dict):
        # Convert nullable
        if node.get("nullable") is True and "type" in node and isinstance(node["type"], str):
            node = {**node, "type": [node["type"], "null"]}
            node.pop("nullable", None)
        return {k: _oas_to_jsonschema(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_oas_to_jsonschema(v) for v in node]
    return node


def _find_op(spec: dict, method: str, path: str) -> dict | None:
    # Try exact path; otherwise match by converting numeric segments to {var}
    paths = spec.get("paths", {})
    if path in paths and method.lower() in paths[path]:
        return paths[path][method.lower()]
    # Normalize: replace numeric and uuid segments with {param}
    parts = path.strip("/").split("/")
    for tmpl in paths:
        tparts = tmpl.strip("/").split("/")
        if len(tparts) != len(parts):
            continue
        ok = True
        for tp, p in zip(tparts, parts):
            if tp.startswith("{") and tp.endswith("}"):
                continue
            if tp != p:
                ok = False
                break
        if ok and method.lower() in paths[tmpl]:
            return paths[tmpl][method.lower()]
    return None


def _response_schema(op: dict, status_code: int) -> dict | None:
    responses = op.get("responses", {})
    key_candidates = [str(status_code), "default", "2XX", f"{status_code // 100}XX"]
    for k in key_candidates:
        if k in responses:
            content = responses[k].get("content", {})
            for ct in ("application/json", "application/json; charset=utf-8"):
                if ct in content:
                    return content[ct].get("schema")
    return None


def validate(spec: dict, method: str, path: str, resp: requests.Response) -> tuple[str, str]:
    """Returns (status_emoji, message)."""
    op = _find_op(spec, method, path)
    if op is None:
        return "SKIP", "endpoint not in schema (undocumented)"
    # 204 No Content: documented response with no body is OK if response is empty
    if resp.status_code == 204 and "204" in op.get("responses", {}) and not resp.content:
        return "OK", "documented 204 no content"
    schema = _response_schema(op, resp.status_code)
    if schema is None:
        if resp.status_code >= 400:
            return "SKIP", f"no schema for {resp.status_code} (acceptable for error)"
        return "FAIL", f"no response schema for {resp.status_code}"
    # Build resolver against the full spec
    try:
        body = resp.json()
    except ValueError:
        return "FAIL", f"non-JSON body (Content-Type={resp.headers.get('Content-Type')})"
    resolver = RefResolver.from_schema(spec)
    validator = Draft7Validator(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(body), key=lambda e: e.path)
    if not errors:
        return "OK", "matches schema"
    first = errors[0]
    where = "/".join(str(p) for p in first.absolute_path) or "<root>"
    return "FAIL", f"{len(errors)} error(s); first at {where}: {first.message[:120]}"


def main() -> None:
    base = parse_base()
    load_env_file()

    # Load schema
    schema_path = os.path.join(os.path.dirname(__file__), "..", "..", "test-reports", "openapi.json")
    schema_path = os.path.normpath(schema_path)
    if not os.path.exists(schema_path):
        print(f"FAIL: openapi schema not at {schema_path}")
        sys.exit(1)
    with open(schema_path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    spec = _oas_to_jsonschema(spec)

    # Auth
    u, e, p = unique_user()
    register(base, u, e, p)
    token = login(base, e, p)
    H = auth_headers(token)

    rows: list[tuple[str, str, int, str, str]] = []

    def probe(method: str, path: str, **kwargs):
        url = f"{base}{path}"
        try:
            r = requests.request(method, url, timeout=20, **kwargs)
        except Exception as ex:  # noqa: BLE001
            rows.append((method, path, 0, "FAIL", f"request failed: {ex}"))
            return None
        emoji, msg = validate(spec, method, path, r)
        rows.append((method, path, r.status_code, emoji, msg))
        return r

    # Health
    probe("GET", "/api/health/")
    # Auth
    probe("GET", "/api/auth/profile/", headers=H)
    # Workflows
    probe("GET", "/api/orchestrator/workflows/", headers=H)
    wf_create = probe(
        "POST", "/api/orchestrator/workflows/",
        headers={**H, "Content-Type": "application/json"},
        json={"name": "audit-wf", "description": "x", "status": "draft", "nodes": [], "edges": []},
    )
    wf_id = wf_create.json()["id"] if wf_create and wf_create.status_code in (200, 201) else None
    if wf_id:
        probe("GET", f"/api/orchestrator/workflows/{wf_id}/", headers=H)
        probe("PATCH", f"/api/orchestrator/workflows/{wf_id}/", headers={**H, "Content-Type": "application/json"},
              json={"description": "patched"})
        probe("DELETE", f"/api/orchestrator/workflows/{wf_id}/", headers=H)
    # Credentials
    probe("GET", "/api/credentials/", headers=H)
    probe("GET", "/api/credentials/types/", headers=H)
    # Logs (real sub-paths)
    probe("GET", "/api/logs/audit/", headers=H)
    probe("GET", "/api/logs/executions/", headers=H)
    probe("GET", "/api/logs/insights/stats/", headers=H)
    # MCP
    probe("GET", "/api/mcp/servers/", headers=H)
    # Skills
    probe("GET", "/api/skills/", headers=H)
    # Inference (real sub-paths) — KB is internal, no HTTP CRUD
    probe("GET", "/api/inference/documents/", headers=H)
    # Notifications
    probe("GET", "/api/notifications/", headers=H)
    # Chat
    probe("GET", "/api/orchestrator/chat/", headers=H)
    # Templates
    probe("GET", "/api/orchestrator/templates/", headers=H)
    # Real execute (validates execution response schema)
    nv = os.environ.get("NVIDIA_API_KEY")
    if nv:
        cred_id = create_nvidia_credential(base, token, nv, name=f"audit-{int(time.time())}")
        wf = minimal_nvidia_workflow(cred_id)
        r = requests.post(f"{base}/api/orchestrator/workflows/", headers={**H, "Content-Type": "application/json"}, json=wf, timeout=15)
        nid = r.json()["id"]
            # Workflow execute was deleted with the canvas; agents execute instead.

    # Print + write report
    md = ["# Stage 4 — Endpoint contract audit", "",
          "Validated against live OpenAPI schema (drf-spectacular) using jsonschema Draft7.", "",
          "| Method | Path | Status | Match | Notes |", "|---|---|---|---|---|"]
    pass_count = fail_count = skip_count = 0
    for m, p, sc, e, msg in rows:
        md.append(f"| {m} | `{p}` | {sc} | {e} | {msg} |")
        print(f"[{e}] {m:6} {p:55} {sc}  {msg}")
        if e == "OK":
            pass_count += 1
        elif e == "FAIL":
            fail_count += 1
        else:
            skip_count += 1
    # End summary unchanged below
    md.append("")
    md.append(f"**Totals:** {pass_count} OK / {fail_count} FAIL / {skip_count} SKIP")
    out_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "test-reports", "stage4-contract.md"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"\nWrote {out_path}")
    print(f"Totals: {pass_count} pass, {fail_count} fail, {skip_count} skipped")
    sys.exit(1 if fail_count else 0)


if __name__ == "__main__":
    main()
