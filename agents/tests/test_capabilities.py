"""
The capability registry (`GET /api/orchestrator/capabilities/`).

Pinned: every grant the runtime serves is described exactly once, the tools
listed are the tools the grant unlocks, and a grant whose engine is `none`
says so rather than rendering a switch for a capability that cannot run.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from agents.agent.runtime import GRANT_TOOLS, UNSERVED_GRANTS

User = get_user_model()


class CapabilityRegistryTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('reader', 'r@example.com', 'pw')
        self.client.force_authenticate(user=self.user)

    def test_it_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse('orchestrator:capability_list'))
        self.assertIn(response.status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_every_served_grant_is_described_with_its_tools(self):
        response = self.client.get(reverse('orchestrator:capability_list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_key = {g['key']: g for g in response.data['grants']}
        self.assertEqual(set(by_key), set(GRANT_TOOLS))
        for key, tools in GRANT_TOOLS.items():
            self.assertEqual(set(by_key[key]['tools']), set(tools), key)
        self.assertEqual(set(response.data['unserved']), set(UNSERVED_GRANTS))

    def test_scope_fields_match_the_runtime_axes(self):
        response = self.client.get(reverse('orchestrator:capability_list'))
        by_key = {g['key']: g for g in response.data['grants']}
        self.assertEqual(by_key['fileOps']['scope'], 'fileAccess')
        self.assertEqual(by_key['mcp']['scope'], 'connectors')
        self.assertEqual(by_key['browser']['scope'], 'browserDomains')
        self.assertIsNone(by_key['codeExecution']['scope'])

    @override_settings(BROWSER_ENGINE='none')
    def test_a_dead_engine_is_reported_not_hidden(self):
        response = self.client.get(reverse('orchestrator:capability_list'))
        by_key = {g['key']: g for g in response.data['grants']}
        self.assertFalse(by_key['browser']['engine_live'])
        self.assertTrue(by_key['browser']['engine_reason'])
        self.assertTrue(by_key['codeExecution']['engine_live'])
