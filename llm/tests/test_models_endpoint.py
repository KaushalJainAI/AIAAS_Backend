"""
Tests for the AI model registry endpoint.

`/api/nodes/models/` is kept as an alias for BrowserOS, which ships its own
build and cannot be redeployed in lockstep with the frontend.

That alias used to be fragile: `nodes.urls` ended in a `nodes/<str:node_type>/`
catch-all, and `models` is a valid `str`, so an include reordering silently sent
the alias to the node-schema view — a dead model picker with nothing in the logs
to explain it. The node-schema routes were deleted with the workflow product, so
the hazard is gone; the tests below pin the alias itself, and that no catch-all
has reappeared above it.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import resolve, reverse
from rest_framework.test import APIClient

from llm.models import AIModel, AIProvider
from llm.providers import SUPPORTED_PROVIDERS
from llm.views import AIModelListView


class LegacyAliasRoutingTests(TestCase):
    """Both paths must reach the same view, whatever the include order is."""

    def test_canonical_route_resolves(self):
        match = resolve('/api/llm/models/')
        self.assertIs(match.func.view_class, AIModelListView)

    def test_legacy_alias_is_not_captured_by_the_node_detail_route(self):
        match = resolve('/api/nodes/models/')
        self.assertIs(
            match.func.view_class, AIModelListView,
            'the /api/nodes/models/ alias must keep resolving for BrowserOS',
        )

    def test_no_catchall_shadows_the_alias(self):
        """A `nodes/<str:...>/` route must never reappear above the alias."""
        from django.urls.exceptions import Resolver404
        with self.assertRaises(Resolver404):
            resolve('/api/nodes/httpRequest/')


class ModelListPayloadTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='picker', email='picker@example.com', password='pw',
        )
        # DRF here authenticates by JWT, not session, so force_authenticate is
        # the only thing that stands in for a logged-in caller.
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        # `nodes.0005` handed these tables over state-only, so rows seeded by
        # earlier migrations may already be present — never assume an empty table.
        self.provider, _ = AIProvider.objects.update_or_create(
            slug='openrouter', defaults={'name': 'OpenRouter', 'is_active': True},
        )
        AIModel.objects.update_or_create(
            value='test/free-model',
            defaults={
                'provider': self.provider, 'name': 'Free Model',
                'is_active': True, 'is_free': True, 'supports_tool_calling': True,
            },
        )

    def test_lists_models_with_capability_flags(self):
        response = self.client.get(reverse('ai-models'))
        self.assertEqual(response.status_code, 200)
        models = [
            m
            for p in response.json()['providers']
            for m in p['models']
        ]
        entry = next(m for m in models if m['value'] == 'test/free-model')
        self.assertTrue(entry['supports_tool_calling'])
        self.assertFalse(entry['supports_image_generation'])
        # Free cloud models are offered before the user configures anything.
        self.assertTrue(entry['available'])

    def test_only_supported_providers_are_offered(self):
        AIProvider.objects.update_or_create(
            slug='retired-provider', defaults={'name': 'Retired', 'is_active': True},
        )
        response = self.client.get(reverse('ai-models'))
        slugs = {p['slug'] for p in response.json()['providers']}
        self.assertNotIn('retired-provider', slugs)
        self.assertTrue(slugs.issubset(set(SUPPORTED_PROVIDERS)))

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(reverse('ai-models')).status_code, 401)
