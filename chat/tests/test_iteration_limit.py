"""
A run that keeps calling tools ends at its iteration cap with an answer, never
with GraphRecursionError.

Found by the work-tier stress benchmark (2026-09-17): the recursion limit was
`max_iterations * 2 + 10`, written when the loop was agent -> tools; with
curate and steering added it is four node visits per iteration, so a long
month-end close died at about half its configured iterations and the
`at_limit` answer path never ran. Drives the real graph with a stubbed model.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase

from chat.turn import agent as agent_module
from chat.turn.agent import TurnContext, run_turn


class _Relentless:
    """Always wants another tool call. `obey_withholding` controls whether it
    stops calling when the runtime offers no tools, as a real provider would."""

    def __init__(self, *, obey_withholding: bool):
        self.obey = obey_withholding
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        index = self.calls
        offered = bool(kwargs.get('tools'))

        async def chunks():
            if offered or not self.obey:
                yield {'type': 'tool_calls', 'tool_calls': [{
                    'index': 0, 'id': f'call-{index}',
                    'function': {'name': 'update_todos',
                                 'arguments': json.dumps({'todos': [{'text': f'step {index}', 'status': 'in_progress'}]})},
                }]}
            else:
                yield {'type': 'content', 'content': 'Stopped at the step limit; here is where I got to.'}
            yield {'type': 'metadata', 'usage': {'total_tokens': 3}}

        return chunks()


async def _never(*_args, **_kwargs) -> bool:
    return False


async def _sink(*_args, **_kwargs) -> None:
    return None


class IterationLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('limit', 'limit@example.com', 'x')

    def run_with(self, model, iterations: int, thread: str):
        turn = TurnContext(
            provider='stub', model='stub-model', system_message='test',
            user_id=self.user.id, session_id=thread, intent='chat', user_text='go',
            memory_enabled=False, max_iterations=iterations,
            approval_policy=_never, sink=_sink,
        )
        with patch('llm.access.stream', new=model):
            return async_to_sync(run_turn)(turn, prompt='go', thread_id=thread)

    def test_a_long_run_reaches_its_cap_and_answers(self):
        model = _Relentless(obey_withholding=True)
        result = self.run_with(model, iterations=20, thread='limit-long')
        self.assertEqual(result.error, '')
        self.assertIn('step limit', result.answer)
        # Every configured iteration was actually available.
        self.assertEqual(model.calls, 20)

    def test_a_model_that_ignores_withheld_tools_still_stops(self):
        result = self.run_with(_Relentless(obey_withholding=False), iterations=6, thread='limit-rogue')
        self.assertEqual(result.error, '')

    def test_the_step_budget_matches_the_graph(self):
        graph = agent_module._build_graph()
        loop_nodes = {'agent', 'tools', 'curate', 'steering'}
        self.assertTrue(loop_nodes <= set(graph.nodes))
        self.assertEqual(agent_module.STEPS_PER_ITERATION, len(set(graph.nodes) - {'__start__'}))
