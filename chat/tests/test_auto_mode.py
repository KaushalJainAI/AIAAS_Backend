"""
Auto mode (P3): a mode picker in chat, a reviewer that may allow clear
matches, argument-shaped trust, and one button that pauses everything.

Pinned: the reviewer only ever downgrades ask → allow (never for spends,
publishes above link, unseen recipients, first-seen browser hosts or tainted
arguments); a dead or slow judge is an ask; plan withholds rather than gates;
trust matches exactly or not at all; a paused account refuses before any model
call.
"""
from __future__ import annotations

import json
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from chat.models import ChatSession, ToolPermission
from chat.tools import READ_ONLY_TOOLS
from chat.tools.permissions import is_remembered
from chat.turn import reviewer
from chat.turn.reviewer import audit_for, auto_policy, read_only_source, review

User = get_user_model()


async def _allow(**kwargs):
    return {'allow': True, 'reason': 'Clearly asked for.'}


async def _deny(**kwargs):
    return {'allow': False, 'reason': 'Not convinced.'}


async def _dead(**kwargs):
    raise RuntimeError('no model here')


class ReviewerStaticTests(TestCase):
    def decide(self, **kwargs):
        params = dict(tool_name='write_file', args={'path': 'a.md'},
                      described='Write a.md', user_text='write a.md to say hi')
        judge = kwargs.pop('judge', _deny)
        params.update(kwargs)
        return async_to_sync(review)(**params, judge=judge)

    def test_a_spendy_tool_always_asks(self):
        out = self.decide(tool_name='generate_image', args={'prompt': 'a cat'},
                          judge=_allow)
        self.assertFalse(out['allow'])
        self.assertEqual(out['reviewed_by'], 'rules')

    def test_publishing_above_link_always_asks(self):
        out = self.decide(tool_name='publish_page',
                          args={'visibility': 'public'}, judge=_allow)
        self.assertFalse(out['allow'])

    def test_a_link_page_may_reach_the_judge(self):
        out = async_to_sync(review)(
            tool_name='publish_page', args={'visibility': 'link'},
            described='Publish a page', user_text='publish it', judge=_allow)
        self.assertTrue(out['allow'])
        self.assertEqual(out['reviewed_by'], 'model')

    def test_a_recipient_outside_the_request_always_asks(self):
        out = self.decide(tool_name='message_send',
                          args={'to': '#strangers', 'message': 'hi'},
                          user_text='tell #team-ops hi', judge=_allow)
        self.assertFalse(out['allow'])
        self.assertIn('#strangers', out['reason'])

    def test_a_recipient_in_the_request_may_reach_the_judge(self):
        out = async_to_sync(review)(
            tool_name='message_send',
            args={'to': '#team-ops', 'message': 'hi'},
            described='Send hi to #team-ops',
            user_text='tell #team-ops hi', judge=_allow)
        self.assertTrue(out['allow'])

    def test_tainted_arguments_always_ask(self):
        out = self.decide(tainted=True, judge=_allow)
        self.assertFalse(out['allow'])

    def test_a_first_seen_browser_host_always_asks(self):
        out = self.decide(tool_name='browser_act',
                          args={'url': 'https://portal.example.com/',
                                'steps': [{'action': 'click', 'selector': 'button'}]},
                          seen_hosts=(), judge=_allow)
        self.assertFalse(out['allow'])

    def test_a_dead_judge_is_an_ask_not_an_allow(self):
        out = async_to_sync(review)(
            tool_name='write_file', args={'path': 'a.md'},
            described='Write a.md', user_text='write a.md', judge=_dead)
        self.assertFalse(out['allow'])

    def test_review_never_raises(self):
        async def _boom(**kwargs):
            raise ValueError('judge exploded')

        out = async_to_sync(review)(
            tool_name='x', args={}, described='x', judge=_boom)
        self.assertFalse(out['allow'])


class AutoPolicyTests(TestCase):
    def test_a_read_runs_without_a_reviewer(self):
        policy = auto_policy(user_text='what time is it')
        self.assertFalse(async_to_sync(policy)('get_current_time', {}, {'user_id': 1}))
        self.assertIsNone(audit_for(policy, 'get_current_time', {}))

    def test_an_allowed_write_runs_and_leaves_an_audit(self):
        policy = auto_policy(user_text='save the notes')
        with _judge(reviewer, _allow):
            paused = async_to_sync(policy)(
                'write_file', {'path': 'n.md'}, {'user_id': 1})
        self.assertFalse(paused)
        audit = audit_for(policy, 'write_file', {'path': 'n.md'})
        self.assertEqual(
            (audit['mode'], audit['verdict'], audit['reviewed_by']),
            ('auto', 'allow', 'model'),
        )
        self.assertTrue(audit['reason'])

    def test_a_refused_write_pauses(self):
        policy = auto_policy(user_text='save the notes')
        with _judge(reviewer, _deny):
            paused = async_to_sync(policy)(
                'write_file', {'path': 'n.md'}, {'user_id': 1})
        self.assertTrue(paused)
        self.assertEqual(audit_for(policy, 'write_file', {'path': 'n.md'})['verdict'], 'ask')


class PlanWithholdsTests(TestCase):
    def test_plan_offers_only_reads(self):
        from inference import vfs

        user = User.objects.create_user('planner', 'p@example.com', 'pw')
        scope = vfs.chat_scope(user)
        source = read_only_source(user_id=user.id, session_key='s', file_scope=scope)
        offered = async_to_sync(source)()
        names = {t['function']['name'] for t in offered}
        self.assertTrue(names)
        self.assertLessEqual(names, set(READ_ONLY_TOOLS))
        self.assertIn('execute_python', names)
        self.assertNotIn('write_file', names)
        self.assertNotIn('generate_image', names)


class TrustMatchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('truster', 't@example.com', 'pw')
        self.ctx = {'user_id': self.user.id, 'session_id': 's1'}

    def _remember(self, tool, match, session_key=''):
        return ToolPermission.objects.create(
            user=self.user, tool_name=tool, session_key=session_key, match=match)

    def test_an_empty_match_allows_regardless_of_arguments(self):
        self._remember('write_file', {})
        self.assertTrue(async_to_sync(is_remembered)(
            'write_file', self.ctx, {'path': 'anything.md'}))

    def test_an_exact_match_allows(self):
        self._remember('message_send', {'to': '#team-ops', 'channel': 'slack'})
        self.assertTrue(async_to_sync(is_remembered)(
            'message_send', self.ctx, {'to': '#team-ops', 'channel': 'slack'}))

    def test_a_partial_match_does_not(self):
        self._remember('message_send', {'to': '#team-ops'})
        self.assertFalse(async_to_sync(is_remembered)(
            'message_send', self.ctx, {'to': '#strangers'}))

    def test_a_session_rule_does_not_leak_into_another_session(self):
        self._remember('write_file', {}, session_key='other-session')
        self.assertFalse(async_to_sync(is_remembered)(
            'write_file', self.ctx, {'path': 'a.md'}))


class PauseAllTests(TestCase):
    def setUp(self):
        from agents.models import SubAgent
        from core.models import UserProfile

        self.user = User.objects.create_user('pauser', 'p@example.com', 'pw')
        self.profile, _ = UserProfile.objects.get_or_create(user=self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='Worker')

    def test_a_paused_account_refuses_before_any_model_call(self):
        from agents.agent.runtime import AgentRunRefused, check_guardrails

        self.profile.paused_until = timezone.now() + timedelta(hours=1)
        self.profile.save(update_fields=['paused_until'])
        with self.assertRaises(AgentRunRefused):
            async_to_sync(check_guardrails)(self.agent, self.user)

    def test_an_expired_pause_runs_again(self):
        from agents.agent.runtime import check_guardrails

        self.profile.paused_until = timezone.now() - timedelta(minutes=1)
        self.profile.save(update_fields=['paused_until'])
        async_to_sync(check_guardrails)(self.agent, self.user)  # does not raise

    def test_no_pause_runs(self):
        from agents.agent.runtime import check_guardrails

        self.profile.paused_until = None
        self.profile.save(update_fields=['paused_until'])
        async_to_sync(check_guardrails)(self.agent, self.user)  # does not raise


class SessionAutonomyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('moder', 'm@example.com', 'pw')

    def test_full_is_not_a_chat_mode(self):
        from chat.serializers import ChatSessionSerializer

        serializer = ChatSessionSerializer(data={'title': 't', 'autonomy': 'full'})
        self.assertFalse(serializer.is_valid())
        serializer = ChatSessionSerializer(data={'title': 't', 'autonomy': 'auto'})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_new_sessions_inherit_the_account_default(self):
        from core.models import UserProfile
        from rest_framework.test import APIRequestFactory, force_authenticate

        from chat.views import ChatSessionViewSet

        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        profile.default_autonomy = 'auto'
        profile.save(update_fields=['default_autonomy'])

        factory = APIRequestFactory()
        request = factory.post('/api/chat/sessions/', {'title': 't'}, format='json')
        force_authenticate(request, user=self.user)
        view = ChatSessionViewSet.as_view({'post': 'create'})
        response = view(request)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            ChatSession.objects.get(id=response.data['id']).autonomy, 'auto')

    def test_an_explicit_mode_beats_the_default(self):
        from core.models import UserProfile
        from rest_framework.test import APIRequestFactory, force_authenticate

        from chat.views import ChatSessionViewSet

        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        profile.default_autonomy = 'auto'
        profile.save(update_fields=['default_autonomy'])

        factory = APIRequestFactory()
        request = factory.post('/api/chat/sessions/', {'title': 't', 'autonomy': 'plan'},
                               format='json')
        force_authenticate(request, user=self.user)
        response = ChatSessionViewSet.as_view({'post': 'create'})(request)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            ChatSession.objects.get(id=response.data['id']).autonomy, 'plan')

    def test_profile_default_is_validated(self):
        from core.serializers import UserProfileSerializer

        serializer = UserProfileSerializer(data={'default_autonomy': 'full'})
        self.assertFalse(serializer.is_valid())


class _judge:
    """Swap the reviewer's model call for a fake within one policy test."""

    def __init__(self, module, fake):
        self.module = module
        self.fake = fake
        self.real = module._model_judge

    def __enter__(self):
        self.module._model_judge = self.fake
        return self

    def __exit__(self, *args):
        self.module._model_judge = self.real
        return False
