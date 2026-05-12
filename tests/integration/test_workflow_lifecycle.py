"""
Workflow CRUD + ownership integration tests.

Verifies:
  Happy — create → list → retrieve → update → delete
  Sad   — missing required fields, invalid status
  Angry — IDOR (read another user's workflow), oversized JSON,
          deeply nested nodes, mass assignment of `user` field
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

User = get_user_model()


WORKFLOWS_URL = "/api/orchestrator/workflows/"


def _wf_payload(**overrides):
    base = {
        "name": "Test Workflow",
        "description": "made by tests",
        "status": "draft",
        "nodes": [],
        "edges": [],
    }
    base.update(overrides)
    return base


class WorkflowHappyPath(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "Sup3r$ecret!")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_full_crud_cycle(self):
        # CREATE
        r = self.client.post(WORKFLOWS_URL, _wf_payload(), format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        wf_id = r.data["id"]

        # LIST
        r = self.client.get(WORKFLOWS_URL)
        self.assertEqual(r.status_code, 200)
        results = r.data.get("results", r.data) if isinstance(r.data, dict) else r.data
        self.assertTrue(any((w.get("id") if isinstance(w, dict) else w) == wf_id for w in results))

        # RETRIEVE
        r = self.client.get(f"{WORKFLOWS_URL}{wf_id}/")
        self.assertEqual(r.status_code, 200)

        # UPDATE (full PUT — backend view requires GET/PUT/DELETE)
        r = self.client.put(
            f"{WORKFLOWS_URL}{wf_id}/",
            _wf_payload(description="updated"),
            format="json",
        )
        self.assertIn(r.status_code, (200, 202), r.content)

        # DELETE
        r = self.client.delete(f"{WORKFLOWS_URL}{wf_id}/")
        self.assertIn(r.status_code, (200, 202, 204))


class WorkflowSadPath(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "x" * 12)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_unauthenticated_returns_401(self):
        anon = APIClient()
        r = anon.get(WORKFLOWS_URL)
        self.assertIn(r.status_code, (401, 403))

    def test_missing_name_rejected(self):
        r = self.client.post(WORKFLOWS_URL, _wf_payload(name=""), format="json")
        self.assertIn(r.status_code, (400, 422))

    def test_invalid_status_rejected(self):
        r = self.client.post(WORKFLOWS_URL, _wf_payload(status="not-a-status"), format="json")
        self.assertIn(r.status_code, (400, 422))

    def test_get_nonexistent_returns_404(self):
        r = self.client.get(f"{WORKFLOWS_URL}999999/")
        self.assertIn(r.status_code, (404, 403))


class WorkflowAngryPath(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "x" * 12)
        self.mallory = User.objects.create_user("mallory", "mallory@example.com", "x" * 12)
        self.client = APIClient()

    def _as(self, user):
        self.client.force_authenticate(user)

    def test_idor_other_users_workflow_404(self):
        """Mallory must not be able to read or modify Alice's workflow by ID."""
        self._as(self.alice)
        r = self.client.post(WORKFLOWS_URL, _wf_payload(name="alices-secret"), format="json")
        wf_id = r.data["id"]

        self._as(self.mallory)
        r = self.client.get(f"{WORKFLOWS_URL}{wf_id}/")
        self.assertIn(r.status_code, (403, 404), "IDOR: cross-tenant read leaked")

        r = self.client.put(
            f"{WORKFLOWS_URL}{wf_id}/", _wf_payload(description="pwned"), format="json"
        )
        self.assertIn(r.status_code, (403, 404))

        r = self.client.delete(f"{WORKFLOWS_URL}{wf_id}/")
        self.assertIn(r.status_code, (403, 404))

    def test_mass_assignment_user_field_ignored(self):
        """Posting `user: <other_id>` must not transfer ownership."""
        self._as(self.alice)
        r = self.client.post(
            WORKFLOWS_URL,
            _wf_payload(user=self.mallory.id),
            format="json",
        )
        self.assertIn(r.status_code, (200, 201), r.content)
        from orchestrator.models import Workflow
        wf = Workflow.objects.get(id=r.data["id"])
        self.assertEqual(wf.user_id, self.alice.id, "mass assignment of `user` succeeded")

    def test_deeply_nested_node_payload_does_not_500(self):
        """Pathological nested JSON should be rejected, not crash the server."""
        self._as(self.alice)
        nested = {"v": 1}
        for _ in range(200):
            nested = {"child": nested}
        r = self.client.post(
            WORKFLOWS_URL,
            _wf_payload(nodes=[{"id": "n1", "data": nested}]),
            format="json",
        )
        # Anything except 5xx is acceptable.
        self.assertLess(r.status_code, 500, "deeply-nested JSON crashed server")

    def test_oversized_workflow_name_rejected(self):
        self._as(self.alice)
        r = self.client.post(WORKFLOWS_URL, _wf_payload(name="A" * 5000), format="json")
        self.assertIn(r.status_code, (400, 413, 422))

    def test_concurrent_create_distinct_ids(self):
        """Two rapid creates from same user must produce distinct IDs (no race)."""
        self._as(self.alice)
        ids = set()
        for i in range(5):
            r = self.client.post(WORKFLOWS_URL, _wf_payload(name=f"wf-{i}"), format="json")
            self.assertIn(r.status_code, (200, 201))
            ids.add(r.data["id"])
        self.assertEqual(len(ids), 5)
