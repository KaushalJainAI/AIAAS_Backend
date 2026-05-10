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
