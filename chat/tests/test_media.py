"""
`generate_image`: the Imagine generation path, as a tool that saves into files.

The provider is faked at `OpenRouterService` — the network is not what is being
tested. What is: the image lands in the caller's scope as a real image file,
unsupported dials are dropped rather than refused, a missing credential says
how to fix it, and the cost is recorded so the spend cap counts it — in chat
(the message's price) and in agent runs (the step row, read back by the run's
cost rollup).
"""
from __future__ import annotations

import base64
import io
import json
import shutil
import tempfile
from decimal import Decimal
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from chat.tools import execute_tool
from chat.tools.media import IMAGE_COST_ESTIMATE_USD
from inference import vfs
from inference.models import Document

User = get_user_model()


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new('RGB', (8, 8), 'teal').save(buf, format='PNG')
    return buf.getvalue()


CAPS = {'image': [{'id': 'google/gemini-3.1-flash-image', 'aspect_ratios': ['1:1', '16:9']}],
        'video': [], 'audio': []}


class _FakeService:
    calls: list = []

    def __init__(self, cost=0.02):
        self.cost = cost

    def generate_image(self, prompt, model, config):
        _FakeService.calls.append((prompt, model, config))
        url = 'data:image/png;base64,' + base64.b64encode(_png()).decode()
        return {'url': url, 'urls': [url], 'cost': self.cost}


class GenerateImageTests(TestCase):
    def setUp(self):
        cache.clear()
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.scope = vfs.chat_scope(self.user)
        _FakeService.calls = []

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)

    def call(self, args, service=None):
        ctx = {'user_id': self.user.id, 'file_scope': self.scope}
        with mock.patch('imagine.services.openrouter.OpenRouterService.for_user',
                        return_value=service or _FakeService()), \
             mock.patch('imagine.services.capabilities.capabilities_for', return_value=CAPS):
            return json.loads(async_to_sync(execute_tool)('generate_image', args, ctx))

    def test_the_image_is_saved_into_the_callers_folder(self):
        out = self.call({'prompt': 'A calm harbour at dawn', 'aspect_ratio': '16:9'})
        self.assertEqual(out['path'], '/Chat/images/a-calm-harbour-at-dawn.png')
        doc = Document.objects.get(id=out['document_id'])
        self.assertEqual(doc.file_type, 'image')
        with doc.file.open('rb') as fh:
            self.assertTrue(fh.read().startswith(b'\x89PNG'))
        # And a deck can embed it by that path.
        data, _ = vfs.read_image(self.scope, out['path'])
        self.assertTrue(data.startswith(b'\x89PNG'))

    def test_an_unsupported_dial_is_dropped_not_refused(self):
        out = self.call({'prompt': 'x', 'aspect_ratio': '9:16'})
        self.assertNotIn('error', out)
        self.assertEqual(_FakeService.calls[0][2], {})

    def test_the_reported_cost_is_recorded(self):
        out = self.call({'prompt': 'x'})
        self.assertEqual((out['cost_usd'], out['cost_source']), ('0.02', 'billed'))

    def test_an_unpriced_image_is_estimated_not_free(self):
        out = self.call({'prompt': 'x'}, service=_FakeService(cost=None))
        self.assertEqual(Decimal(out['cost_usd']), IMAGE_COST_ESTIMATE_USD)
        self.assertEqual(out['cost_source'], 'estimated')

    def test_a_missing_credential_says_how_to_fix_it(self):
        from imagine.services.openrouter import MissingOpenRouterCredentialError

        ctx = {'user_id': self.user.id, 'file_scope': self.scope}
        with mock.patch('imagine.services.openrouter.OpenRouterService.for_user',
                        side_effect=MissingOpenRouterCredentialError('No OpenRouter key.')):
            out = json.loads(async_to_sync(execute_tool)('generate_image', {'prompt': 'x'}, ctx))
        self.assertIn('Credentials page', out['error'])

    def test_it_spends_money_so_it_is_irreversible(self):
        from chat.tools.registry import get

        self.assertEqual(get('generate_image').effect, 'irreversible')


class ImageCostReachesTheBillTests(TestCase):
    def test_chat_prices_the_turn_with_the_image(self):
        from chat.turn.agent import _apply_side_effects

        meta = {}

        async def sink(event, payload):
            return None

        result = {'path': '/Chat/images/x.png', 'document_id': 1, 'created': True,
                  'type': 'image', 'bytes': 10, 'cost_usd': '0.03', 'cost_source': 'billed'}
        async_to_sync(_apply_side_effects)('generate_image', {}, json.dumps(result), meta, sink)
        self.assertEqual(meta['tool_costs'], [{'tool': 'generate_image', 'cost_usd': '0.03',
                                               'cost_source': 'billed'}])
        self.assertEqual(meta['files'][0]['type'], 'image')

    def test_an_agent_run_rolls_the_step_cost_into_the_run(self):
        from agents.agent.runtime import _roll_up_cost
        from logs.models import AgentStep, ExecutionLog

        user = User.objects.create_user('runner', 'runner@example.com', 'pw')
        log = ExecutionLog.objects.create(user=user, status='running')
        for cost in ('0.03', '0.05'):
            AgentStep.objects.create(
                execution=log, call_id=f'c{cost}', tool='generate_image', status='completed',
                result={'result': json.dumps({'cost_usd': cost, 'cost_source': 'billed'})},
            )
        _roll_up_cost(log)
        self.assertEqual(log.cost_usd, Decimal('0.08'))
