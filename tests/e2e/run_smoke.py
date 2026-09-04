#!/usr/bin/env python
"""
End-to-end smoke test against a running backend.

Verifies the golden path is alive: health → register → login → workflow CRUD.
Exit code 0 = all green, 1 = at least one failure.
"""
from __future__ import annotations

import os
import time

import requests

from _lib import (
    Runner,
    auth_headers,
    create_nvidia_credential,
    load_env_file,
    login,
    minimal_nvidia_workflow,
    parse_base,
    register,
    unique_user,
)


def main() -> None:
    base = parse_base()
    load_env_file()
    r = Runner(base)

    def health():
        resp = requests.get(f"{base}/api/health/", timeout=5)
        assert resp.status_code == 200, resp.status_code
        assert resp.json().get("status") == "healthy"

    state: dict = {}

    def do_register():
        u, e, p = unique_user()
        state["email"] = e
        state["password"] = p
        resp = register(base, u, e, p)
        assert resp.status_code in (200, 201), f"{resp.status_code} {resp.text[:200]}"
        return f"user={u}"

    def do_login():
        state["token"] = login(base, state["email"], state["password"])
        return "got token"

    def create_wf():
        resp = requests.post(
            f"{base}/api/orchestrator/workflows/",
            headers=auth_headers(state["token"]),
            json={
                "name": "e2e smoke",
                "description": "auto",
                "status": "draft",
                "nodes": [],
                "edges": [],
            },
            timeout=15,
        )
        assert resp.status_code in (200, 201), f"{resp.status_code} {resp.text[:200]}"
        state["wf_id"] = resp.json()["id"]
        return f"id={state['wf_id']}"

    def get_wf():
        resp = requests.get(
            f"{base}/api/orchestrator/workflows/{state['wf_id']}/",
            headers=auth_headers(state["token"]),
            timeout=10,
        )
        assert resp.status_code == 200, resp.status_code

    def patch_wf():
        resp = requests.patch(
            f"{base}/api/orchestrator/workflows/{state['wf_id']}/",
            headers=auth_headers(state["token"]),
            json={"description": "updated"},
            timeout=10,
        )
        assert resp.status_code in (200, 202), f"{resp.status_code} {resp.text[:200]}"

    def delete_wf():
        resp = requests.delete(
            f"{base}/api/orchestrator/workflows/{state['wf_id']}/",
            headers=auth_headers(state["token"]),
            timeout=10,
        )
        assert resp.status_code in (200, 202, 204), resp.status_code

    # The real-LLM execution step lived here. It drove
    # `POST /api/orchestrator/workflows/{id}/execute/` and polled
    # `/executions/{id}/status/`, both deleted with the workflow canvas. The
    # live equivalent is `POST /api/orchestrator/agents/{id}/execute/`, which
    # answers 202 and streams to `ws/execution/{id}/` instead of returning 200
    # for polling — a different contract, so this is a port, not a rename.
    # TODO: re-add against the agents endpoint.

    r.step("GET /api/health/", health)
    r.step("POST /api/auth/register/", do_register)
    r.step("POST /api/auth/login/", do_login)
    r.step("POST /api/orchestrator/workflows/", create_wf)
    r.step("GET  /api/orchestrator/workflows/{id}/", get_wf)
    r.step("PATCH /api/orchestrator/workflows/{id}/", patch_wf)
    r.step("DELETE /api/orchestrator/workflows/{id}/", delete_wf)

    r.report_and_exit()


if __name__ == "__main__":
    main()
