"""
An agent configured on a dead model still runs — on the fallback — and the
owner is told once.

Two properties carry this: substitution happens at resolve time, before
preflight, so a retired id never reaches the wire; and the stored
configuration is never rewritten, so the run record (`model_used` /
`fallback_from`) is the honest account of what the run did about it.
"""
from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TransactionTestCase

from agents.models import SubAgent
from chat.turn.agent import TurnResult
from llm.models import AIModel, AIProvider
from logs.models import ExecutionLog
from notifications.models import Notification


async def completed_turn(turn, *, prompt, thread_id):
    return TurnResult(answer='done')


async def no_preflight(**kwargs):
    return None


class FallbackRunTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='fallbacker', password='pw')
        provider = AIProvider.objects.create(
            name='OpenRouter', slug='openrouter')
        AIModel.objects.create(
            provider=provider, name='Busted', value='old/busted',
            is_active=False)
        AIModel.objects.create(
            provider=provider, name='Shiny', value='new/shiny',
            is_active=True)
        self.agent = SubAgent.objects.create(
            user=self.user, name='Reporter', prompt='Report.',
            tool_grants={}, guardrails={},
            llm_provider='openrouter', llm_model='old/busted')

    def tearDown(self):
        cache.clear()

    async def _run(self, **kwargs):
        from agents.agent.runtime import run_agent

        events = []

        async def sink(event, payload):
            events.append((str(event), payload))

        kwargs.setdefault('sink', sink)
        with patch('chat.turn.agent.run_turn', completed_turn), \
                patch('llm.access.preflight', no_preflight):
            result = await run_agent(
                self.agent, 'go', user=self.user,
                thread_id='thread-fallback', **kwargs)
        return result, events

    async def test_a_retired_model_runs_on_the_fallback(self):
        result, events = await self._run()
        self.assertEqual(result.answer, 'done')

        log = await ExecutionLog.objects.aget(
            subagent=self.agent, thread_id='thread-fallback')
        self.assertEqual(log.status, 'completed')
        self.assertEqual(log.model_used, 'openrouter/free')
        self.assertEqual(log.fallback_from, 'old/busted')

        # The watching caller is told on the stream, in the existing STATUS
        # vocabulary — no new wire event for old clients to choke on.
        phases = [p for e, p in events if e == 'status']
        self.assertTrue(
            any(p.get('phase') == 'model_fallback' for p in phases))

    async def test_the_config_is_never_rewritten_and_the_owner_is_told_once(self):
        await self._run()
        await self._run()

        await sync_to_async(self.agent.refresh_from_db)()
        self.assertEqual(self.agent.llm_model, 'old/busted')

        notes = await sync_to_async(list)(Notification.objects.filter(
            user=self.user, type='system'))
        fallback_notes = [
            n for n in notes if (n.data or {}).get('kind') == 'model_fallback']
        self.assertEqual(len(fallback_notes), 1)
        note = fallback_notes[0]
        self.assertIn('old/busted', note.message)
        self.assertIn('openrouter/free', note.message)
        self.assertEqual(note.data.get('action_url'),
                         f'/agents/{self.agent.id}')

    async def test_an_active_model_runs_untouched_with_no_notice(self):
        await sync_to_async(
            lambda: setattr(self.agent, 'llm_model', 'new/shiny') or
            self.agent.save(update_fields=['llm_model']))()
        result, events = await self._run()
        self.assertEqual(result.answer, 'done')
        log = await ExecutionLog.objects.aget(
            subagent=self.agent, thread_id='thread-fallback')
        self.assertEqual(log.fallback_from, '')
        self.assertFalse(await Notification.objects.filter(
            user=self.user).aexists())
        self.assertFalse(any(
            p.get('phase') == 'model_fallback'
            for e, p in events if e == 'status'))

    async def test_a_provider_410_mid_run_still_notifies(self):
        """The catalogue said live but the provider answered gone.

        No mid-run model switch — that would silently change provenance —
        but the owner is still told once through the same notice, so the fix
        is one builder visit away.
        """
        from llm.access import LLMModelUnavailable

        await sync_to_async(
            lambda: setattr(self.agent, 'llm_model', 'new/shiny') or
            self.agent.save(update_fields=['llm_model']))()

        async def refused_turn(turn, *, prompt, thread_id):
            raise LLMModelUnavailable('"new/shiny" is no longer available.')

        from agents.agent.runtime import run_agent

        with patch('chat.turn.agent.run_turn', refused_turn), \
                patch('llm.access.preflight', no_preflight):
            with self.assertRaises(LLMModelUnavailable):
                await run_agent(self.agent, 'go', user=self.user,
                                thread_id='thread-race')

        log = await ExecutionLog.objects.aget(
            subagent=self.agent, thread_id='thread-race')
        self.assertEqual(log.status, 'failed')
        notes = await sync_to_async(list)(Notification.objects.filter(
            user=self.user, type='system'))
        self.assertTrue(any(
            (n.data or {}).get('kind') == 'model_fallback' for n in notes))
