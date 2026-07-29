"""
Adversarial tests against the orchestrator app (workflows + executions).

Targets known dangerous patterns: IDOR, mass assignment, oversized payloads,
status-machine bypasses, and webhook spoofing.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from executor.trigger_manager import TriggerManager, TriggerRegistryUnavailable
from orchestrator.models import Workflow

User = get_user_model()


class WorkflowModelAngry(TestCase):
    """Direct model-level adversarial tests (no HTTP)."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "a@x.com", "x" * 12)
        self.mallory = User.objects.create_user("m", "m@x.com", "x" * 12)

    def test_workflow_must_have_a_user(self):
        from django.db import IntegrityError
        with self.assertRaises((IntegrityError, ValueError)):
            Workflow.objects.create(name="orphan", nodes=[], edges=[])

    def test_huge_nodes_payload_persists_or_rejected(self):
        """100K-node JSON should either persist cleanly or be rejected — never
        partially saved."""
        nodes = [{"id": f"n{i}", "type": "http", "data": {}} for i in range(100_000)]
        try:
            wf = Workflow.objects.create(user=self.alice, name="big", nodes=nodes, edges=[])
            wf.refresh_from_db()
            self.assertEqual(len(wf.nodes), 100_000)
        except Exception:
            # Acceptable: DB / serializer rejected. The point is no partial state.
            self.assertFalse(Workflow.objects.filter(name="big").exists())


class WebhookAngry(TestCase):
    """The public webhook endpoint is the highest-risk attack surface."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "a@x.com", "x" * 12)
        self.client = APIClient()
        # These tests are about how the *view* handles hostile input, so the
        # registry is stubbed to "nothing is registered". Left unstubbed the
        # lookup would hit a Redis that isn't running under test settings, and
        # every case would error on the connection instead of exercising the
        # path it was written for.
        patcher = patch.object(TriggerManager, "lookup_webhook", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_webhook_for_nonexistent_user_does_not_500(self):
        # /api/webhooks/<user_id>/<path>
        r = self.client.post("/api/webhooks/999999/anything", {"x": 1}, format="json")
        self.assertLess(r.status_code, 500)

    def test_webhook_with_huge_body_does_not_500(self):
        body = {"data": "X" * (1024 * 100)}  # 100 KB
        r = self.client.post(
            f"/api/webhooks/{self.alice.id}/test", body, format="json"
        )
        self.assertLess(r.status_code, 500)

    def test_webhook_path_traversal_attempt(self):
        r = self.client.post(
            f"/api/webhooks/{self.alice.id}/../../admin", {}, format="json"
        )
        # Django URL resolver should normalise — anything but 5xx is OK.
        self.assertLess(r.status_code, 500)

    def test_registry_outage_is_503_not_404(self):
        """
        A Redis outage must not be reported as "no such webhook". Senders like
        GitHub disable a hook that keeps 404ing, so answering 404 here would
        turn a transient outage into a permanently unhooked integration.
        """
        with patch.object(
            TriggerManager, "lookup_webhook",
            side_effect=TriggerRegistryUnavailable("connection refused"),
        ):
            r = self.client.post(
                f"/api/webhooks/{self.alice.id}/test", {"x": 1}, format="json"
            )
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r["Retry-After"], "30")

    def test_registry_outage_does_not_leak_internals(self):
        """The 503 body is for an untrusted caller — no host names, no stack."""
        with patch.object(
            TriggerManager, "lookup_webhook",
            side_effect=TriggerRegistryUnavailable("Error 111 connecting to redis-prod:6379"),
        ):
            r = self.client.post(
                f"/api/webhooks/{self.alice.id}/test", {}, format="json"
            )
        self.assertNotIn("redis-prod", r.content.decode())
        self.assertNotIn("6379", r.content.decode())


class HealthCheckTests(TestCase):
    """Tiny smoke + adversarial checks on /api/health/."""

    def setUp(self):
        self.client = APIClient()

    def test_health_returns_200(self):
        r = self.client.get("/api/health/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "healthy")

    def test_health_method_not_allowed_or_ok_post(self):
        r = self.client.post("/api/health/", {})
        # Either 200 (permissive) or 405 — not 500.
        self.assertIn(r.status_code, (200, 405))
