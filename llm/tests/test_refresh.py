"""
Live catalogue refresh: what it writes, what it refuses, and who it tells.

The three properties that carry the design: nothing is ever deleted (a saved
run or agent may still reference the id); an empty or unreachable upstream
never blanks the table (the Imagine rule); and one refresh notifies each
owner once no matter how often it runs (`ModelFallbackNotice`).
"""
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from llm import catalog_refresh as _refresh
from llm.catalog_refresh import (
    REFRESH_LOCK_KEY,
    RefreshError,
    RefreshInProgress,
)
from llm.models import AIModel, AIProvider, ModelFallbackNotice
from agents.models import SubAgent
from notifications.models import Notification


def live_entry(value, *, prompt='0.000002', completion='0.000006',
               context=1000000, modalities=None, params=None, name=None):
    return {
        'id': value,
        'name': name or value,
        'context_length': context,
        'pricing': {'prompt': prompt, 'completion': completion},
        'architecture': {'input_modalities': modalities or ['text']},
        'supported_parameters': params or [],
    }


class ParseEntryTests(SimpleTestCase):
    def test_variant_ids_are_skipped(self):
        for value in ('x/y:free', 'x/y:batch', 'x/y~alias', 'x/y:nitro'):
            self.assertIsNone(
                _refresh._parse_entry(live_entry(value)), value)

    def test_prices_are_per_token_converted_to_per_million(self):
        parsed = _refresh._parse_entry(live_entry('a/b'))
        self.assertEqual(parsed['input_price_per_million'], Decimal('2.0000'))
        self.assertEqual(parsed['output_price_per_million'], Decimal('6.0000'))

    def test_zero_zero_is_free_anything_else_is_not(self):
        free = _refresh._parse_entry(
            live_entry('a/f', prompt='0', completion='0'))
        paid = _refresh._parse_entry(live_entry('a/p'))
        self.assertTrue(free['is_free'])
        self.assertFalse(paid['is_free'])

    def test_caps_only_claim_what_the_listing_evidences(self):
        parsed = _refresh._parse_entry(live_entry(
            'a/v', modalities=['text', 'image'],
            params=['tools', 'structured_outputs']))
        self.assertTrue(parsed['caps']['supports_image_input'])
        self.assertTrue(parsed['caps']['supports_tool_calling'])
        self.assertTrue(parsed['caps']['supports_structured_output'])
        self.assertFalse(parsed['caps']['supports_video_input'])

    def test_a_blank_id_is_skipped(self):
        self.assertIsNone(_refresh._parse_entry(live_entry('')))


class SuggestSuccessorTests(SimpleTestCase):
    def test_known_rename_wins(self):
        self.assertEqual(
            _refresh._suggest_successor(
                'qwen/qwen3.8-max', {'qwen/qwen3.8-max-0902'}),
            'qwen/qwen3.8-max-0902')

    def test_prefix_match_finds_the_base_snapshot(self):
        self.assertEqual(
            _refresh._suggest_successor('acme/x', {'acme/x-0902', 'other/y'}),
            'acme/x-0902')

    def test_no_match_is_blank_not_a_guess(self):
        self.assertEqual(
            _refresh._suggest_successor('acme/x', {'other/y'}), '')


class RefreshApplyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='refresher', password='pw')
        self.provider = AIProvider.objects.create(
            name='OpenRouter', slug='openrouter')
        self.kept = AIModel.objects.create(
            provider=self.provider, name='Kept', value='keep/me',
            input_price_per_million=Decimal('1.0000'),
            output_price_per_million=Decimal('2.0000'),
            context_window=1000)
        self.gone = AIModel.objects.create(
            provider=self.provider, name='Gone', value='gone/model')
        self.hand = AIModel.objects.create(
            provider=self.provider, name='Hand', value='hand/made',
            source='hand')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Reporter', llm_provider='openrouter',
            llm_model='gone/model')

    def tearDown(self):
        cache.clear()

    def _run(self, entries):
        with patch.object(_refresh, '_fetch_live', return_value=entries), \
                patch.object(_refresh, '_resolve_key', return_value='k'):
            return _refresh.refresh_catalog(user=self.user)

    def test_update_retire_add_and_spare_hand(self):
        summary = self._run([
            live_entry('keep/me', prompt='0.000003',
                       completion='0.000006', context=2000),
            live_entry('brand/new', modalities=['text', 'image'],
                       params=['tools']),
        ])
        self.kept.refresh_from_db()
        self.assertEqual(summary['updated'], 1)
        self.assertEqual(self.kept.input_price_per_million, Decimal('3.0000'))
        self.assertEqual(self.kept.context_window, 2000)
        self.assertIsNotNone(self.kept.last_seen_at)

        self.gone.refresh_from_db()
        self.assertFalse(self.gone.is_active)
        self.assertIsNotNone(self.gone.retired_at)
        self.assertEqual(
            [r['value'] for r in summary['retired']], ['gone/model'])

        # Discovered upstream, gated by staff: present but not offered.
        new = AIModel.objects.get(value='brand/new')
        self.assertFalse(new.is_active)
        self.assertEqual(new.source, 'live')
        self.assertTrue(new.supports_image_input)
        self.assertTrue(new.supports_tool_calling)
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['new_upstream'], ['brand/new'])

        # A person's row is never the refresh's to remove.
        self.hand.refresh_from_db()
        self.assertTrue(self.hand.is_active)

    def test_a_model_back_upstream_is_relisted(self):
        self.gone.refresh_from_db()
        self._run([live_entry('gone/model')])
        self.gone.refresh_from_db()
        self.assertTrue(self.gone.is_active)
        self.assertIsNone(self.gone.retired_at)

    def test_a_forced_id_retires_even_when_live_lists_it(self):
        from populate_models import RETIRED_MODEL_VALUES

        forced = RETIRED_MODEL_VALUES[0]
        row = AIModel.objects.create(
            provider=self.provider, name='Forced', value=forced)
        self._run([live_entry('keep/me'), live_entry(forced)])
        row.refresh_from_db()
        self.assertFalse(row.is_active)

    def test_owner_is_notified_once_with_config_untouched(self):
        self._run([live_entry('keep/me')])
        notes = Notification.objects.filter(
            user=self.user, type='system')
        self.assertEqual(notes.count(), 1)
        note = notes.first()
        self.assertEqual(note.data.get('kind'), 'model_retired')
        self.assertEqual(note.data.get('action_url'), '/agents')
        self.assertIn('gone/model', note.message)
        self.assertTrue(ModelFallbackNotice.objects.filter(
            subagent=self.agent, old_value='gone/model').exists())
        # The run path substitutes at resolve time; the stored config stays
        # what the owner chose.
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.llm_model, 'gone/model')

        # A second refresh finds nothing newly retired and stays quiet.
        with patch.object(_refresh, '_fetch_live',
                          return_value=[live_entry('keep/me')]), \
                patch.object(_refresh, '_resolve_key', return_value='k'):
            _refresh.refresh_catalog(user=self.user)
        self.assertEqual(
            Notification.objects.filter(user=self.user).count(), 1)

    def test_lock_contention_is_409_not_a_second_diff(self):
        cache.add(REFRESH_LOCK_KEY, True, timeout=180)
        with self.assertRaises(RefreshInProgress):
            self._run([live_entry('keep/me')])
        self.gone.refresh_from_db()
        self.assertTrue(self.gone.is_active)

    def test_no_key_writes_nothing(self):
        with patch.object(_refresh, '_fetch_live',
                          return_value=[live_entry('keep/me')]), \
                patch.object(_refresh, '_resolve_key', return_value=None):
            with self.assertRaises(RefreshError):
                _refresh.refresh_catalog(user=self.user)
        self.gone.refresh_from_db()
        self.assertTrue(self.gone.is_active)

    def test_empty_upstream_never_blanks_the_table(self):
        with patch.object(_refresh, '_fetch_live', return_value=[]), \
                patch.object(_refresh, '_resolve_key', return_value='k'):
            with self.assertRaises(RefreshError):
                _refresh.refresh_catalog(user=self.user)
        self.assertTrue(AIModel.objects.filter(is_active=True).exists())

    def test_status_is_cached_for_the_picker_meta(self):
        self._run([live_entry('keep/me')])
        status = cache.get(_refresh.REFRESH_STATUS_KEY)
        self.assertIsNotNone(status)
        self.assertEqual(status['by'], 'refresher')
        self.assertEqual(
            [r['value'] for r in status['retired']], ['gone/model'])
