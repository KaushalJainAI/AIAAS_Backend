"""
A chat session naming a retired model answers on the fallback — and heals.

Same rule agent runs apply, at the same point (before preflight): the turn
executes on the platform fallback, the swap is announced on the stream, and
the session default is persisted to whatever the turn used, so the next turn
starts clean. The stored choice is never the dead id again.
"""
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from chat.models import ChatSession
from chat.turn.agent import TurnResult
from chat.turn.pipeline import TurnRequest, run_chat_turn
from llm.models import AIModel, AIProvider


class ChatFallbackTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.user = User.objects.create_user(username='chatfb', password='pw')
        provider = AIProvider.objects.create(
            name='OpenRouter', slug='openrouter')
        AIModel.objects.create(
            provider=provider, name='Busted', value='old/busted',
            is_active=False)
        self.session = ChatSession.objects.create(
            user=self.user, title='T', llm_provider='openrouter',
            llm_model='old/busted', memory_enabled=False)
        self.events: list[tuple] = []
        self.seen: dict = {}

    def tearDown(self) -> None:
        cache.clear()

    async def _sink(self, event, payload) -> None:
        self.events.append((event, payload))

    @staticmethod
    async def _none(*_a, **_k) -> list:
        return []

    def _run(self):
        async def capture_turn(turn, *, prompt, thread_id, **kwargs):
            self.seen['model'] = turn.model
            self.seen['provider'] = turn.provider
            return TurnResult(answer='hi')

        async def no_preflight(**kwargs):
            return None

        with patch('chat.turn.agent.run_turn', capture_turn), \
                patch('llm.access.preflight', no_preflight), \
                patch('chat.turn.agent.suggest_follow_ups', self._none):
            return async_to_sync(run_chat_turn)(
                session=self.session,
                user=self.user,
                request=TurnRequest.parse({'content': 'hello'}),
                sink=self._sink,
            )

    def test_a_retired_session_model_answers_on_the_fallback(self):
        outcome = self._run()
        self.assertEqual(outcome.assistant_message.content, 'hi')
        self.assertEqual(self.seen.get('model'), 'openrouter/free')
        self.assertEqual(self.seen.get('provider'), 'openrouter')
        phases = [p for e, p in self.events if str(e) == 'status']
        self.assertTrue(any(
            p.get('phase') == 'model_fallback' for p in phases))

    def test_the_session_heals_to_whatever_the_turn_used(self):
        self._run()
        self.session.refresh_from_db()
        self.assertEqual(self.session.llm_model, 'openrouter/free')
