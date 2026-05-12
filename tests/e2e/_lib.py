"""Tiny shared helpers for the e2e scripts. No external deps beyond requests."""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from dataclasses import dataclass

import requests


def parse_base() -> str:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--base", default=os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    args, _ = p.parse_known_args()
    return args.base.rstrip("/")


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        flag = "PASS" if self.ok else "FAIL"
        return f"[{flag}] {self.name}{(' — ' + self.detail) if self.detail else ''}"


class Runner:
    def __init__(self, base: str):
        self.base = base
        self.results: list[Result] = []

    def step(self, name: str, fn):
        t0 = time.time()
        try:
            detail = fn() or ""
            self.results.append(Result(name, True, f"{detail} ({(time.time()-t0)*1000:.0f}ms)"))
        except AssertionError as e:
            self.results.append(Result(name, False, str(e)))
        except Exception as e:  # noqa: BLE001
            self.results.append(Result(name, False, f"{type(e).__name__}: {e}"))

    def report_and_exit(self) -> None:
        for r in self.results:
            print(r.line())
        failed = [r for r in self.results if not r.ok]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} passed")
        sys.exit(1 if failed else 0)


def unique_user() -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:10]
    username = f"e2e_{suffix}"
    email = f"e2e_{suffix}@example.com"
    password = "Sup3r$ecret-e2e!"
    return username, email, password


def register(base: str, username: str, email: str, password: str) -> requests.Response:
    return requests.post(
        f"{base}/api/auth/register/",
        json={"username": username, "email": email, "password": password, "password2": password},
        timeout=15,
    )


def login(base: str, email: str, password: str) -> str:
    r = requests.post(
        f"{base}/api/auth/login/",
        json={"email": email, "password": password},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    token = body.get("access") or body.get("access_token")
    assert token, f"login response missing access token: {body}"
    return token


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def load_env_file(path: str = None) -> None:
    """Load Backend/.env into os.environ if not already set. No deps."""
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.normpath(os.path.join(here, "..", "..", ".env"))
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def create_credential(base: str, token: str, slug: str, api_key: str, name: str) -> int:
    """Create an API-key credential of the given type. Returns credential id."""
    r = requests.post(
        f"{base}/api/credentials/",
        headers={**auth_headers(token), "Content-Type": "application/json"},
        json={
            "name": name,
            "credential_type": slug,
            "data": {"api_key": api_key},
        },
        timeout=15,
    )
    assert r.status_code in (200, 201), f"credential create failed: {r.status_code} {r.text[:200]}"
    return r.json()["id"]


def create_nvidia_credential(base: str, token: str, api_key: str, name: str = "e2e-nvidia") -> int:
    return create_credential(base, token, "nvidia", api_key, name)


def minimal_nvidia_workflow(credential_id: int) -> dict:
    """Trivial manual_trigger → nvidia workflow that asks for one word."""
    return {
        "name": "e2e-min-nvidia",
        "description": "auto",
        "status": "draft",
        "nodes": [
            {
                "id": "trig1",
                "type": "manual_trigger",
                "data": {"nodeType": "manual_trigger", "config": {}},
                "position": {"x": 0, "y": 0},
            },
            {
                "id": "nv1",
                "type": "nvidia",
                "data": {
                    "nodeType": "nvidia",
                    "config": {
                        "credential": credential_id,
                        "credential_id": credential_id,
                        "model": "nvidia/llama-3.3-nemotron-super-49b-v1",
                        "prompt": "Reply with exactly: pong",
                        "temperature": 0.0,
                        "max_tokens": 16,
                    },
                },
                "position": {"x": 200, "y": 0},
            },
        ],
        "edges": [
            {"id": "e1", "source": "trig1", "target": "nv1"},
        ],
    }
