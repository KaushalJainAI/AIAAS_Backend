"""
The cost ledger (`logs.CostEntry` + `logs/costs.py::record`).

`_tool_costs` reading `AgentStep.result` was right for one priced tool
(images). Pinned here: the ledger is the record going forward, the spend cap
sums tokens + ledger rows, and one image is never charged twice — the `image`
kind already rides inside `ExecutionLog.cost_usd`, so the aggregate excludes
it while every other kind counts only here.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from decimal import Decimal
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from agents.spend import aggregate_rupees
from agents.models import SubAgent
from inference import vfs
from logs.costs import record
from logs.models import CostEntry, ExecutionLog

User = get_user_model()


class LedgerRecordTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('ledger', 'ledger@example.com', 'pw')

    def test_record_writes_one_row(self):
        entry = record(user=self.user, kind='sms', amount_inr=5, units=2,
                       unit='messages', estimated=False, source='message_send:c1')
        self.assertIsNotNone(entry)
        self.assertEqual(CostEntry.objects.count(), 1)
        row = CostEntry.objects.get()
        self.assertEqual((row.kind, row.amount_inr, row.estimated), ('sms', 5, False))

    def test_zero_is_not_a_charge(self):
        self.assertIsNone(record(user=self.user, kind='sms', amount_inr=0))
        self.assertEqual(CostEntry.objects.count(), 0)

    def test_dedupe_key_keeps_resume_from_charging_twice(self):
        log = ExecutionLog.objects.create(user=self.user, status='running')
        for _ in range(2):
            record(user=self.user, kind='image', amount_inr=3, execution=log,
                   units=1, unit='image', estimated=False,
                   source='generate_image:c9', dedupe_key='generate_image:c9')
        self.assertEqual(CostEntry.objects.filter(execution=log).count(), 1)

    def test_a_failed_write_returns_none_instead_of_raising(self):
        self.assertIsNone(record(user=None, kind='sms', amount_inr=5))


class SpendCapCountsLedgerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('spender', 'spender@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Spender')

    def _log(self, **kwargs):
        return ExecutionLog.objects.create(user=self.user, subagent=self.agent,
                                           status='completed', **kwargs)

    def test_a_ledger_only_kind_counts_on_top_of_tokens(self):
        from agents.spend import rupees_for

        log = self._log(tokens_used=0, cost_usd=Decimal('0'), cost_source='')
        record(user=self.user, kind='sms', amount_inr=7, execution=log,
               units=3, unit='messages', estimated=True, source='message_send:c1')
        self.assertEqual(aggregate_rupees(ExecutionLog.objects.filter(id=log.id)), 7)
        self.assertEqual(rupees_for(0), 0)  # the tokens half is genuinely zero

    def test_an_image_is_counted_once_not_twice(self):
        from agents.spend import rupees_for_usd

        log = self._log(cost_usd=Decimal('0.020000'), cost_source='billed',
                        tokens_used=0)
        record(user=self.user, kind='image', amount_inr=2, execution=log,
               units=1, unit='image', estimated=False, source='generate_image:c1',
               dedupe_key='generate_image:c1')
        self.assertEqual(
            aggregate_rupees(ExecutionLog.objects.filter(id=log.id)),
            rupees_for_usd(Decimal('0.02')),
        )


class ImageLedgerMoveTests(TestCase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.scope = vfs.chat_scope(self.user)

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)

    def test_a_generation_files_a_ledger_row_and_keeps_its_result_cost(self):
        import base64
        import io

        from chat.tools import execute_tool

        from PIL import Image

        buf = io.BytesIO()
        Image.new('RGB', (8, 8), 'teal').save(buf, format='PNG')
        url = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
        service = mock.Mock()
        service.generate_image.return_value = {'url': url, 'urls': [url], 'cost': 0.02}
        caps = {'image': [{'id': 'google/gemini-3.1-flash-image',
                           'aspect_ratios': ['1:1', '16:9']}],
                'video': [], 'audio': []}
        ctx = {'user_id': self.user.id, 'file_scope': self.scope, 'call_id': 'img-1'}
        with mock.patch('imagine.services.openrouter.OpenRouterService.for_user',
                        return_value=service), \
             mock.patch('imagine.services.capabilities.capabilities_for',
                        return_value=caps):
            out = json.loads(async_to_sync(execute_tool)(
                'generate_image', {'prompt': 'a harbour'}, ctx))
        # The result cost stays: the chat turn prices from it and the rollup
        # sums it. The ledger row is the same charge in rupees for the cap.
        self.assertEqual((out['cost_usd'], out['cost_source']), ('0.02', 'billed'))
        row = CostEntry.objects.get(user=self.user, kind='image')
        self.assertEqual(row.source, 'generate_image:img-1')
        self.assertFalse(row.estimated)
        self.assertGreater(row.amount_inr, 0)
