"""
OpenCode Zen as a fifth provider: bring-your-own-key free models.

No network anywhere here. The one live fact this file depends on — the eight
free ids existing on `GET https://opencode.ai/zen/v1/models` — was verified
2026-09-22 (see `docs/OPENCODE_ZEN_PLAN.md` §8); chat completions, streaming
and `tool_calls` still need a real key.
"""
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from llm.handlers.llm_providers import OpenCodeZenNode
from llm.handlers.registry import get_registry


class SupportedSetTests(SimpleTestCase):
    def test_opencode_is_supported_and_not_default(self):
        from llm.providers import SUPPORTED_PROVIDERS

        self.assertIn('opencode', SUPPORTED_PROVIDERS)
        # Ordering is load-bearing: the first entry is the default, and a
        # bring-your-own-key provider can never be a default.
        self.assertEqual(SUPPORTED_PROVIDERS[0], 'openrouter')
        self.assertEqual(SUPPORTED_PROVIDERS[-1], 'opencode')


class RegistryTests(SimpleTestCase):
    def test_registry_resolves_opencode(self):
        handler = get_registry().get_handler('opencode')
        self.assertIsInstance(handler, OpenCodeZenNode)
        self.assertEqual(handler.node_type, 'opencode')


class WirePrefixTests(SimpleTestCase):
    def test_prefix_is_stripped_on_the_wire(self):
        node = OpenCodeZenNode()
        payload = node.chat_payload(
            model='opencode/big-pickle',
            messages=[{'role': 'user', 'content': 'hi'}],
            config={}, stream=False,
        )
        # Zen wants the bare id; `opencode/big-pickle` 404s upstream.
        self.assertEqual(payload['model'], 'big-pickle')

    def test_default_model_carries_the_prefix(self):
        # So a blank config still resolves through the catalogue namespace.
        self.assertTrue(
            OpenCodeZenNode.default_model.startswith('opencode/'))


class NoPlatformKeyTests(SimpleTestCase):
    def test_no_platform_key_ever(self):
        # Pins the ToS decision: Zen limits a key to its holder's own use, so
        # a platform key would serve third parties. Nobody may "helpfully"
        # add one — not even an oddly-named env var.
        from credentials.resolution import PLATFORM_ENV_KEYS, platform_api_key

        self.assertNotIn('opencode', PLATFORM_ENV_KEYS)
        with patch.dict(os.environ, {
            'OPENCODE_API_KEY': 'sk-test',
            'OPENCODE_ZEN_API_KEY': 'sk-test',
            'ZEN_API_KEY': 'sk-test',
        }):
            self.assertIsNone(platform_api_key('opencode'))


class PickerAvailabilityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='zenfan', email='zenfan@example.com', password='pw')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        from llm.models import AIModel, AIProvider

        self.provider, _ = AIProvider.objects.update_or_create(
            slug='opencode', defaults={'name': 'OpenCode Zen', 'is_active': True},
        )
        AIModel.objects.update_or_create(
            value='opencode/big-pickle',
            defaults={'provider': self.provider, 'name': 'Big Pickle',
                      'is_active': True, 'is_free': True},
        )

    def _opencode_models(self):
        return [m for p in self.client.get(reverse('ai-models')).json()['providers']
                if p['slug'] == 'opencode' for m in p['models']]

    def test_free_model_unavailable_without_user_key(self):
        # No platform key exists for opencode by design, so a free Zen model
        # without the user's own key is offered-but-unrunnable — the exact bug
        # the `available` rule exists to prevent.
        models = self._opencode_models()
        self.assertTrue(models)
        for m in models:
            self.assertFalse(m['available'])

    def test_free_model_available_with_user_key(self):
        from credentials.manager import get_credential_manager
        from credentials.models import Credential, CredentialType

        get_credential_manager()._cache.clear()
        cred_type, _ = CredentialType.objects.update_or_create(
            slug='opencode',
            defaults={'name': 'OpenCode Zen', 'auth_method': 'api_key',
                      'fields_schema': [{'name': 'apiKey'}]},
        )
        cred = Credential.objects.create(
            user=self.user, credential_type=cred_type, name='Zen',
            is_active=True, is_verified=True,
        )
        cred.set_credential_data({'apiKey': 'zen-test'})
        cred.save()
        models = self._opencode_models()
        self.assertTrue(models)
        for m in models:
            self.assertTrue(m['available'])


class MissingKeyPreflightTests(TestCase):
    def test_missing_key_fails_at_preflight(self):
        """No key → immediate account error, before any status event."""
        from chat.models import ChatSession
        from chat.turn.pipeline import TurnError, TurnRequest, run_chat_turn
        from asgiref.sync import async_to_sync

        user = get_user_model().objects.create_user(
            username='zenbroke', password='pw')
        session = ChatSession.objects.create(
            user=user, title='T', llm_provider='opencode',
            llm_model='opencode/big-pickle')
        events: list = []

        async def sink(event, payload):
            events.append((event, payload))

        # No catalogue rows in this database, so the fallback resolver passes
        # the pair through untouched — the failure below is the missing key,
        # not a substitution.
        with self.assertRaises(TurnError) as caught:
            async_to_sync(run_chat_turn)(
                session=session, user=user,
                request=TurnRequest.parse({'content': 'hello'}),
                sink=sink,
            )
        self.assertIn('opencode', str(caught.exception).lower())
        self.assertEqual(events, [])


class CredentialTypeSeededTests(TestCase):
    def test_credential_type_seeded_by_migrations(self):
        # A fresh test database is migrations-only state — the same state a
        # fresh `migrate` yields — so this is the test that fails if
        # migrations stop being sufficient.
        from credentials.models import CredentialType

        self.assertTrue(
            CredentialType.objects.filter(slug='opencode').exists())
