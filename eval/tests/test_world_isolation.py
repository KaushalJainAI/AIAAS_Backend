"""
An eval world tests the agent that really runs, and touches nothing real.

Pins the fixes from the 2026-09-25 review of the eval-environments build:
simulated tools obey the agent's connector scope and per-tool deny; tools
that reach the owner's account are withheld (notify is simulated); hidden
world KBs never reach a picker; cases not built for a world run outside it;
and a sweep stays on the world it opened with.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from agents.models import SubAgent
from eval import environment as envmod
from eval.models import EvalCase, EvalSuite, EvalWorld
from eval.tests.test_sim_worlds import EVENTS, MESSAGES


def _gmail_id() -> int:
    from mcp_integration.models import MCPServer

    return MCPServer.objects.get(type='native', icon_slug='gmail').id


class WorldFixture(TestCase):
    connectors: list = []
    permissions: dict = {}

    def setUp(self):
        self.user = User.objects.create_user('iso', 'iso@x.io', 'pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Triage',
            tool_grants={'mcp': True, 'fileOps': True},
            sandbox={'fileAccess': 'scoped'},
            guardrails={'autonomy': 'ask'},
            agent_context={'connectors': self._connectors(),
                           'toolPermissions': dict(self.permissions)},
            prompt='Triage the inbox.')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='Inbox', subagent=self.agent)
        self.world = EvalWorld.objects.create(
            suite=self.suite, version=1, status='accepted', brief='Acme inbox.',
            surfaces={'files': True, 'mail': True, 'calendar': True},
            fixtures={'files': {'notes.md': 'x\n'},
                      'mail': {'messages': MESSAGES},
                      'calendar': {'events': EVENTS}},
            facts=[{'key': 'k', 'value': 'Friday', 'statement': 's'}])

    def _connectors(self):
        return list(self.connectors)

    def _box(self):
        from agents.agent.runtime import AgentToolbox

        env = envmod.for_attempt(self.user, self.agent, self.suite, None)
        env.prepare(None, None, 'r1')
        box = AgentToolbox.for_agent(
            self.agent, self.user.id, file_scope=env.attempt_scope(),
            session_key='t', environment=env)
        return box, env

    def _names(self, box):
        return {d['function']['name'] for d in async_to_sync(box.descriptors)()}


class ConnectorScopeHoldsInWorldTests(WorldFixture):
    """A Gmail-only, read-only agent must not send or touch a calendar."""

    def _connectors(self):
        return [{'id': _gmail_id(), 'mode': 'read'}]

    def test_out_of_scope_tools_are_not_offered(self):
        box, _ = self._box()
        names = self._names(box)
        self.assertIn('gmail_search_threads', names)
        self.assertNotIn('gmail_send_message', names)
        self.assertNotIn('calendar_create_event', names)
        self.assertNotIn('calendar_list_events', names)

    def test_out_of_scope_tools_are_refused_at_dispatch(self):
        box, env = self._box()
        reply = async_to_sync(box.dispatch)(
            'gmail_send_message', {'to': 'a@b.c', 'subject': 's', 'body': 'b'},
            {'user_id': self.user.id})
        self.assertIn('Error', reply)
        self.assertEqual(env.snapshot_env()['mail']['outbox'], [])

    def test_surfaces_follow_the_scope(self):
        from eval.generator import connector_slugs_in_scope, surfaces_for_agent

        surfaces = surfaces_for_agent(self.agent, connector_slugs_in_scope(self.agent))
        self.assertTrue(surfaces.get('mail'))
        self.assertNotIn('calendar', surfaces)
        self.assertNotIn('drive', surfaces)


class DenyHoldsInWorldTests(TestCase):
    """A simulated built-in (`web_search`) still obeys a per-tool deny.

    Connector tools cannot carry a per-tool permission at all
    (`tool_permissions_for` keeps built-ins only; the connector scope governs
    them), so the web world is where deny meets a simulator.
    """

    def test_denied_simulated_tool_is_refused(self):
        from agents.agent.runtime import AgentToolbox

        user = User.objects.create_user('deny', 'deny@x.io', 'pw')
        agent = SubAgent.objects.create(
            user=user, name='Researcher', tool_grants={'webSearch': True},
            sandbox={'fileAccess': 'none'}, guardrails={'autonomy': 'ask'},
            agent_context={'toolPermissions': {'web_search': 'deny'}})
        suite = EvalSuite.objects.create(user=user, name='Web', subagent=agent)
        page = {'url': 'https://acme.test/p', 'title': 'P', 'text': 'Price 5.'}
        EvalWorld.objects.create(
            suite=suite, version=1, status='accepted', brief='b',
            surfaces={'web': True},
            fixtures={'files': {}, 'web': {'pages': [page],
                                           'results': {'price': [page['url']]}}},
            facts=[{'key': 'k', 'value': '5', 'statement': 's'}])
        env = envmod.for_attempt(user, agent, suite, None)
        env.prepare(None, None, 'r1')
        box = AgentToolbox.for_agent(agent, user.id, file_scope=env.attempt_scope(),
                                     session_key='t', environment=env)
        reply = async_to_sync(box.dispatch)('web_search', {'query': 'price'},
                                            {'user_id': user.id})
        self.assertIn("'deny'", reply)
        self.assertEqual(env.snapshot_env()['web']['queries'], [])


class RealAccountToolsWithheldTests(WorldFixture):
    def test_account_tools_are_withheld(self):
        box, _ = self._box()
        names = self._names(box)
        for name in ('schedule_notification', 'cancel_scheduled_notification',
                     'list_scheduled_notifications', 'save_dashboard',
                     'list_user_runs', 'mission_status'):
            self.assertNotIn(name, names)
        for name in ('update_todos', 'ask_user', 'notify_user'):
            self.assertIn(name, names)

    def test_notify_is_simulated_not_sent(self):
        from notifications.models import Notification

        box, env = self._box()
        reply = json.loads(async_to_sync(box.dispatch)(
            'notify_user', {'title': 'Done', 'message': 'Triage finished.'},
            {'user_id': self.user.id}))
        self.assertTrue(reply['sent'])
        self.assertFalse(Notification.objects.filter(user=self.user).exists())
        self.assertEqual(env.changes({})['notify']['notified'][0]['title'], 'Done')


class HiddenKbPickerTests(TestCase):
    def setUp(self):
        from inference.models import KnowledgeBase

        self.user = User.objects.create_user('kbp', 'kbp@x.io', 'pw')
        self.mine = KnowledgeBase.objects.create(user=self.user, name='Mine')
        self.hidden = KnowledgeBase.objects.create(
            user=self.user, name='.eval/s1/v1', backend='raw')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_prefix_is_one_value(self):
        from eval.kb_world import KB_NAME_PREFIX
        from inference.models import HIDDEN_KB_PREFIX

        self.assertEqual(KB_NAME_PREFIX, HIDDEN_KB_PREFIX)

    def test_visible_knowledge_bases_hides_world_corpora(self):
        from inference.models import visible_knowledge_bases

        names = set(visible_knowledge_bases(self.user).values_list('name', flat=True))
        self.assertEqual(names, {'Mine'})

    def test_agent_cannot_be_attached_to_a_world_corpus(self):
        from agents.views.agents import AgentSerializer
        from rest_framework.exceptions import ValidationError

        ser = AgentSerializer(context={'request': type('R', (), {'user': self.user})()})
        self.assertEqual(ser.validate_knowledgeBases([self.mine.id]), [self.mine.id])
        with self.assertRaises(ValidationError):
            ser.validate_knowledgeBases([self.hidden.id])

    def test_reserved_name_is_refused(self):
        from inference.serializers import KnowledgeBaseSerializer

        ser = KnowledgeBaseSerializer(
            data={'name': '.eval/sneaky'},
            context={'request': type('R', (), {'user': self.user})()})
        self.assertFalse(ser.is_valid())
        self.assertIn('name', ser.errors)


class NonWorldCasesRunOutsideTests(WorldFixture):
    def test_case_without_a_version_gets_no_environment(self):
        case = EvalCase.objects.create(suite=self.suite, goal='Summarise my Q3 report.')
        self.assertIsNone(envmod.for_attempt(self.user, self.agent, self.suite, case))

    def test_case_built_for_the_world_gets_it(self):
        case = EvalCase.objects.create(suite=self.suite, goal='g', world_version=1)
        env = envmod.for_attempt(self.user, self.agent, self.suite, case)
        self.assertEqual(env.world.version, 1)


class PinnedWorldTests(WorldFixture):
    def test_sweep_keeps_the_world_it_opened_with(self):
        from eval import runner

        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user)
        self.assertEqual(run.world_version, 1)
        # A newer world is accepted mid-sweep: the sweep must not follow it.
        EvalWorld.objects.create(
            suite=self.suite, version=2, status='accepted', brief='new',
            surfaces={'files': True}, fixtures={'files': {'a.md': 'a'}},
            facts=[{'key': 'k', 'value': 'a', 'statement': 's'}])
        pinned = async_to_sync(runner._pinned_world)(run, self.suite)
        self.assertEqual(pinned.version, 1)
        case = EvalCase.objects.create(suite=self.suite, goal='g', world_version=1)
        env = envmod.for_attempt(self.user, self.agent, self.suite, case, pinned)
        self.assertEqual(env.world.version, 1)
