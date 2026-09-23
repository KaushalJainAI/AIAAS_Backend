"""
Tests for the AI model registry endpoint.

The `/api/nodes/models/` legacy alias was removed with BrowserOS
(PRODUCTIVITY_SUITE_PLAN.md P0). Only the canonical `/api/llm/models/`
remains; the tests below pin that the alias is gone and that no catch-all
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
    """Canonical path resolves; legacy alias is retired."""

    def test_canonical_route_resolves(self):
        match = resolve('/api/llm/models/')
        self.assertIs(match.func.view_class, AIModelListView)

    def test_legacy_alias_is_retired(self):
        from django.urls.exceptions import Resolver404
        with self.assertRaises(Resolver404):
            resolve('/api/nodes/models/')

    def test_no_catchall_shadows_the_alias(self):
        """A `nodes/<str:...>/` route must never reappear above the alias."""
        from django.urls.exceptions import Resolver404
        with self.assertRaises(Resolver404):
            resolve('/api/nodes/httpRequest/')


class ModelListPayloadTests(TestCase):
    def setUp(self):
        import os
        from unittest.mock import patch

        from django.core.cache import cache
        # `get_fallback` caches for 60s with no invalidation hook, so a
        # fallback edited by another test class in the same process leaks
        # here. Clear it, per the witness-cached convention.
        cache.clear()

        # `available` for a free model means "a platform key can actually pay
        # for it" — so pin the key instead of inheriting whatever the machine
        # running the suite happens to have in its environment.
        env = patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-key'})
        env.start()
        self.addCleanup(env.stop)
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

    def test_effort_support_is_reported_per_model(self):
        AIModel.objects.update_or_create(
            value='test/reasoner',
            defaults={
                'provider': self.provider, 'name': 'Reasoner', 'is_active': True,
                'is_free': True, 'effort_levels': ['high', 'low'],
                'default_effort': 'low',
            },
        )
        models = {
            m['value']: m
            for p in self.client.get(reverse('ai-models')).json()['providers']
            for m in p['models']
        }
        entry = models['test/reasoner']
        # Ladder order, not the order the row happened to store them in — the
        # picker renders this list directly and wants cheapest first.
        self.assertEqual(entry['effort_levels'], ['low', 'high'])
        self.assertEqual(entry['default_effort'], 'low')
        self.assertTrue(entry['supports_effort'])

    def test_a_model_without_effort_control_says_so_rather_than_omitting_it(self):
        entry = next(
            m
            for p in self.client.get(reverse('ai-models')).json()['providers']
            for m in p['models']
            if m['value'] == 'test/free-model'
        )
        # `[]` is an answer — "this model has no effort control" — so the key is
        # always present. A missing key would make the picker guess.
        self.assertEqual(entry['effort_levels'], [])
        self.assertFalse(entry['supports_effort'])

    def test_a_drifted_row_never_offers_a_rung_the_runtime_would_refuse(self):
        # The column is admin-editable. A picker rendering a level the runtime
        # would snap away from is worse than one rendering none.
        AIModel.objects.update_or_create(
            value='test/drifted',
            defaults={
                'provider': self.provider, 'name': 'Drifted', 'is_active': True,
                'is_free': True, 'effort_levels': ['low', 'banana'],
                'default_effort': 'nonsense',
            },
        )
        entry = next(
            m
            for p in self.client.get(reverse('ai-models')).json()['providers']
            for m in p['models']
            if m['value'] == 'test/drifted'
        )
        self.assertEqual(entry['effort_levels'], ['low'])
        self.assertEqual(entry['default_effort'], '')

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(reverse('ai-models')).status_code, 401)

    def test_meta_carries_fallback_and_last_refresh(self):
        payload = self.client.get(reverse('ai-models')).json()
        self.assertIn('meta', payload)
        self.assertEqual(
            payload['meta']['fallback'],
            {'provider': 'openrouter', 'model': 'openrouter/free'})
        # No refresh has ever run in this database.
        self.assertIsNone(payload['meta']['last_refresh'])
        # And the old shape still reads: backend-only deploys keep working.
        self.assertIn('providers', payload)


class ModelRefreshEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='refreshfan', password='pw')
        self.staff = User.objects.create_user(
            username='refreshstaff', password='pw', is_staff=True)
        self.client = APIClient()

    def test_non_staff_is_refused(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(reverse('ai-models-refresh'))
        self.assertEqual(response.status_code, 403)

    def test_staff_triggers_a_refresh(self):
        from unittest.mock import patch

        self.client.force_authenticate(user=self.staff)
        summary = {'added': 1, 'updated': 2, 'retired': [],
                   'new_upstream': ['x/y'], 'affected_agents': [],
                   'retired_values': ['z/z']}
        with patch('llm.catalog_refresh.refresh_catalog',
                   return_value=summary):
            response = self.client.post(reverse('ai-models-refresh'))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['added'], 1)
        # Internal bookkeeping never reaches the client.
        self.assertNotIn('retired_values', payload)

    def test_lock_contention_is_409_and_refusal_is_400(self):
        from unittest.mock import patch

        from llm.catalog_refresh import RefreshError, RefreshInProgress

        self.client.force_authenticate(user=self.staff)
        with patch('llm.catalog_refresh.refresh_catalog',
                   side_effect=RefreshInProgress('busy')):
            response = self.client.post(reverse('ai-models-refresh'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'refresh_in_progress')
        with patch('llm.catalog_refresh.refresh_catalog',
                   side_effect=RefreshError('no key')):
            response = self.client.post(reverse('ai-models-refresh'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'refresh_refused')


class ModelFallbackEndpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='fallbackfan', password='pw')
        self.staff = User.objects.create_user(
            username='fallbackstaff', password='pw', is_staff=True)
        self.client = APIClient()
        self.provider, _ = AIProvider.objects.update_or_create(
            slug='openrouter', defaults={'name': 'OpenRouter', 'is_active': True},
        )
        AIModel.objects.update_or_create(
            value='new/shiny',
            defaults={'provider': self.provider, 'name': 'Shiny',
                      'is_active': True},
        )

    def test_anyone_reads_the_fallback(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('model-fallback'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'provider': 'openrouter', 'model': 'openrouter/free'})

    def test_non_staff_cannot_change_it(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.patch(
            reverse('model-fallback'),
            {'provider': 'openrouter', 'model': 'new/shiny'}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_staff_sets_it_and_a_typo_is_refused(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            reverse('model-fallback'),
            {'provider': 'openrouter', 'model': 'typo/model'}, format='json')
        self.assertEqual(response.status_code, 400)
        response = self.client.patch(
            reverse('model-fallback'),
            {'provider': 'openrouter', 'model': 'new/shiny'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(reverse('model-fallback')).json(),
            {'provider': 'openrouter', 'model': 'new/shiny'})

    def test_blank_model_and_unknown_provider_are_400(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            reverse('model-fallback'),
            {'provider': 'openrouter', 'model': ''}, format='json')
        self.assertEqual(response.status_code, 400)
        response = self.client.patch(
            reverse('model-fallback'),
            {'provider': 'nope', 'model': 'new/shiny'}, format='json')
        self.assertEqual(response.status_code, 400)
