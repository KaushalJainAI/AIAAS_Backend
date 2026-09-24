"""
The `auto` reviewer, as it actually runs (2026-09-24).

Every test in `test_auto_mode.py` swaps `_model_judge` for a fake, and that is
how `import llm; llm.complete(...)` — an `AttributeError` on every call, since
the funnel is `llm.access` — shipped: the judge never ran once in production,
`review` read the exception as "reviewer unavailable", and `auto` asked about
everything. These tests pin the real wiring and the four fixes that followed:

- the judge resolves through `llm.access.complete`, on the configured
  reviewer model rather than the chat's;
- a verdict is reached once per call, so a resumed batch is not re-judged;
- gates are decided concurrently, and an approved call is never re-gated;
- the reviewer reads recent user messages and the live plan, and says it is
  reviewing while it does.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings
from langchain_core.messages import AIMessage

from chat.turn import reviewer
from chat.turn.agent import TurnContext, tools_node
from chat.turn.pipeline import REVIEWER_RECENT_USER_MESSAGES, reviewer_text
from chat.turn.reviewer import auto_policy


class _Completion(SimpleNamespace):
    pass


def _fake_complete(reply: str, seen: list):
    async def complete(**kwargs):
        seen.append(kwargs)
        return _Completion(content=reply)
    return complete


class RealJudgeWiringTests(SimpleTestCase):
    """No `_model_judge` fake here: this is the path production takes."""

    def judge(self, **kwargs):
        params = dict(described='Write notes.md', user_text='save notes.md',
                      todos=(), user_id=1, provider='openrouter',
                      model='openrouter/free')
        params.update(kwargs)
        return async_to_sync(reviewer._model_judge)(**params)

    @override_settings(AUTO_REVIEWER_PROVIDER='openrouter',
                       AUTO_REVIEWER_MODEL='meta-llama/llama-4-scout')
    def test_the_judge_calls_the_real_funnel_on_the_reviewer_model(self):
        seen: list = []
        with patch('llm.access.complete', _fake_complete('ALLOW it matches', seen)):
            allow, reason = self.judge()
        self.assertTrue(allow)
        self.assertEqual(reason, 'it matches')
        self.assertEqual((seen[0]['provider'], seen[0]['model']),
                         ('openrouter', 'meta-llama/llama-4-scout'))
        self.assertEqual(seen[0]['effort'], 'none')

    @override_settings(AUTO_REVIEWER_PROVIDER='openrouter', AUTO_REVIEWER_MODEL='')
    def test_a_blank_model_falls_back_to_the_chats_whole_pair(self):
        seen: list = []
        with patch('llm.access.complete', _fake_complete('ASK unsure', seen)):
            allow, _ = self.judge(provider='openai', model='gpt-x')
        self.assertFalse(allow)
        # Never the configured provider with the chat's model: that pair names
        # a model the provider does not serve.
        self.assertEqual((seen[0]['provider'], seen[0]['model']), ('openai', 'gpt-x'))

    def test_review_reaches_a_verdict_through_the_real_judge(self):
        seen: list = []
        with patch('llm.access.complete', _fake_complete('ALLOW yes', seen)):
            out = async_to_sync(reviewer.review)(
                tool_name='write_file', args={'path': 'notes.md'},
                described='Write notes.md', user_text='save notes.md')
        self.assertEqual((out['allow'], out['reviewed_by']), (True, 'model'))


class VerdictCacheTests(SimpleTestCase):
    def setUp(self):
        reviewer._verdicts.clear()
        self.calls: list = []

        async def judge(**kwargs):
            self.calls.append(kwargs)
            return {'allow': True, 'reason': 'ok'}

        self.real = reviewer._model_judge
        reviewer._model_judge = judge

    def tearDown(self):
        reviewer._model_judge = self.real
        reviewer._verdicts.clear()

    def gate(self, policy, call_id, **extra):
        ctx = {'user_id': 1, 'session_id': 's1', 'call_id': call_id, **extra}
        return async_to_sync(policy)('write_file', {'path': 'n.md'}, ctx)

    def test_the_same_call_is_judged_once(self):
        policy = auto_policy(user_text='save n.md')
        self.assertFalse(self.gate(policy, 'c1'))
        # A second policy object, as a resumed turn builds: still cached.
        self.assertFalse(self.gate(auto_policy(user_text='save n.md'), 'c1'))
        self.assertEqual(len(self.calls), 1)

    def test_a_different_call_is_judged_again(self):
        policy = auto_policy(user_text='save n.md')
        self.gate(policy, 'c1')
        self.gate(policy, 'c2')
        self.assertEqual(len(self.calls), 2)

    def test_the_live_plan_beats_the_turns_snapshot(self):
        policy = auto_policy(user_text='save n.md', todos=[{'text': 'old'}])
        self.gate(policy, 'c1', todos=[{'status': 'in_progress', 'text': 'new'}])
        self.assertEqual(self.calls[0]['todos'][0]['text'], 'new')

    def test_reviewing_is_announced_on_the_sink(self):
        frames: list = []

        async def sink(event, payload):
            frames.append((str(getattr(event, 'value', event)), payload))

        self.gate(auto_policy(user_text='save n.md'), 'c1', sink=sink)
        self.assertEqual(frames[0][0], 'status')
        self.assertEqual(frames[0][1]['phase'], 'reviewing')


class ReviewerTextTests(SimpleTestCase):
    def msg(self, role, content):
        return SimpleNamespace(role=role, content=content)

    def test_recent_user_messages_come_first_and_the_question_last(self):
        past = [self.msg('user', f'u{i}') for i in range(6)] + [self.msg('assistant', 'a')]
        lines = reviewer_text(past, 'yes, send it').split('\n')
        self.assertEqual(len(lines), REVIEWER_RECENT_USER_MESSAGES)
        self.assertEqual(lines[-1], 'yes, send it')
        self.assertEqual(lines[0], 'u3')
        self.assertNotIn('a', lines)

    def test_a_recipient_named_earlier_reaches_the_rules(self):
        text = reviewer_text([self.msg('user', 'reply to john@acme.com')], 'send it')
        self.assertIsNone(reviewer.static_check(
            tool_name='message_send', args={'to': 'john@acme.com'}, user_text=text))

    def test_memory_off_is_the_message_alone(self):
        self.assertEqual(reviewer_text([], 'hi'), 'hi')


def _turn(policy) -> TurnContext:
    async def dispatch(name, args, context):
        return 'ok'

    return TurnContext(
        provider='test', model='test-model', system_message='sys',
        user_id=1, session_id='thread-1', intent='chat', user_text='q',
        tool_dispatch=dispatch, sensitive_tools=frozenset(),
        approval_policy=policy,
    )


def _run(calls, turn, metadata=None):
    state = {
        'messages': [AIMessage(content='', tool_calls=calls)],
        'metadata': dict(metadata or {}), 'tool_trace': [], 'thinking': '',
        'total_tokens': 0,
    }

    async def _no_images(*_a, **_k):
        return []

    with patch('chat.sources.search.image_search', _no_images):
        return async_to_sync(tools_node)(state, {'configurable': {'turn': turn}})


class GateSettlingTests(SimpleTestCase):
    def test_gates_are_decided_concurrently(self):
        delay = 0.25

        async def slow_policy(name, args, context):
            await asyncio.sleep(delay)
            return False

        calls = [{'name': 'write_file', 'id': f'c{i}', 'args': {'path': f'{i}.md'}}
                 for i in range(4)]
        started = time.monotonic()
        _run(calls, _turn(slow_policy))
        # Sequential would be 4 x 0.25 = 1.0 s before dispatch even starts.
        self.assertLess(time.monotonic() - started, delay * 2.5)

    def test_an_approved_call_is_not_gated_again(self):
        asked: list = []

        async def policy(name, args, context):
            asked.append(context['call_id'])
            return False

        calls = [{'name': 'write_file', 'id': 'c1', 'args': {'path': 'a.md'}},
                 {'name': 'write_file', 'id': 'c2', 'args': {'path': 'b.md'}}]
        _run(calls, _turn(policy), metadata={'approved_tool_calls': ['c1']})
        self.assertEqual(asked, ['c2'])

    def test_the_policy_sees_the_call_id_and_the_live_plan(self):
        seen: list = []

        async def policy(name, args, context):
            seen.append(context)
            return False

        _run([{'name': 'write_file', 'id': 'c9', 'args': {'path': 'a.md'}}],
             _turn(policy), metadata={'todos': [{'text': 'ship', 'status': 'pending'}]})
        self.assertEqual(seen[0]['call_id'], 'c9')
        self.assertEqual(seen[0]['todos'][0]['text'], 'ship')
