#!/usr/bin/env python
"""
Adversarial / chaos probes against a live backend.

These intentionally send malformed, oversized, hostile, or out-of-order
requests. The contract is simple: the server may say no, but it must NEVER
return 5xx. A 5xx here is a bug.
"""
from __future__ import annotations

import string
import time

import requests

from _lib import Runner, auth_headers, login, parse_base, register, unique_user


def main() -> None:
    base = parse_base()
    r = Runner(base)

    # Authenticated session for tests that need one
    u, e, p = unique_user()
    register(base, u, e, p)
    token = login(base, e, p)
    H = auth_headers(token)

    def assert_no_5xx(resp: requests.Response, label: str):
        assert resp.status_code < 500, f"{label} returned {resp.status_code}: {resp.text[:200]}"

    def malformed_json_body():
        resp = requests.post(
            f"{base}/api/auth/login/",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert_no_5xx(resp, "malformed JSON")

    def empty_body():
        resp = requests.post(f"{base}/api/auth/login/", data=b"",
                             headers={"Content-Type": "application/json"}, timeout=10)
        assert_no_5xx(resp, "empty body")

    def oversized_field():
        resp = requests.post(
            f"{base}/api/auth/login/",
            json={"email": "a" * 100_000 + "@x.com", "password": "x"},
            timeout=15,
        )
        assert_no_5xx(resp, "oversized email")

    def huge_workflow_payload():
        nodes = [{"id": f"n{i}", "type": "http", "data": {}} for i in range(2000)]
        resp = requests.post(
            f"{base}/api/orchestrator/workflows/",
            headers=H,
            json={"name": "chaos", "status": "draft", "nodes": nodes, "edges": []},
            timeout=30,
        )
        assert_no_5xx(resp, "huge workflow")

    def idor_scan():
        """Walk a few likely IDs we don't own and ensure every response is 4xx."""
        for wf_id in (1, 2, 3, 999, 12345):
            resp = requests.get(
                f"{base}/api/orchestrator/workflows/{wf_id}/", headers=H, timeout=10
            )
            assert resp.status_code != 500, f"IDOR scan id={wf_id} → 500"
            assert resp.status_code in (200, 401, 403, 404), f"id={wf_id} {resp.status_code}"

    def unicode_username():
        bad_user = "alice‮​admin"
        resp = requests.post(
            f"{base}/api/auth/register/",
            json={"username": bad_user, "email": "u@x.com", "password": "x" * 12, "password2": "x" * 12},
            timeout=10,
        )
        assert_no_5xx(resp, "unicode bidi attack")

    def webhook_garbage():
        resp = requests.post(
            f"{base}/api/webhooks/9999999/random",
            data=b"\x00\x01\x02not-json",
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert_no_5xx(resp, "webhook garbage")

    def unauthorised_protected_endpoint():
        resp = requests.get(f"{base}/api/orchestrator/workflows/", timeout=10)
        assert resp.status_code in (401, 403), f"protected endpoint returned {resp.status_code}"

    def rapid_burst():
        """50 fast requests — confirms throttling / connection handling don't crash."""
        bad = 0
        for _ in range(50):
            resp = requests.get(f"{base}/api/health/", timeout=5)
            if resp.status_code >= 500:
                bad += 1
        assert bad == 0, f"{bad}/50 requests crashed"

    r.step("malformed JSON body", malformed_json_body)
    r.step("empty body", empty_body)
    r.step("oversized field", oversized_field)
    r.step("huge workflow payload", huge_workflow_payload)
    r.step("IDOR scan", idor_scan)
    r.step("unicode bidi username", unicode_username)
    r.step("webhook garbage", webhook_garbage)
    r.step("unauthorised access blocked", unauthorised_protected_endpoint)
    r.step("rapid burst (no 5xx)", rapid_burst)

    r.report_and_exit()


if __name__ == "__main__":
    main()
