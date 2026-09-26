"""
P5–P8: compute, code, missions, dashboards — and the tools-list limits.

Covers what the plan phases added after P4: the `compute`/`shell` grants,
their scopes, the engine-gated offering, the mission caller, the dashboard
spec validation, and the new TOOL_SETTINGS knobs actually moving a tool.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from agents.agent.runtime import code_projects_for, workspace_egress_for
from agents.grants import GRANT_TOOLS
from tools_config.overlay import limits

User = get_user_model()


class PhaseGrantsTests(TestCase):
    def test_compute_and_shell_are_grant_gated(self):
        self.assertIn('compute', GRANT_TOOLS)
        self.assertIn('shell', GRANT_TOOLS)
        self.assertEqual(
            set(GRANT_TOOLS['compute']),
            {'workspace_exec', 'start_job', 'job_status', 'job_logs',
             'cancel_job', 'sync_files'})
        self.assertIn('ws_run', GRANT_TOOLS['shell'])
        self.assertIn('git_push', GRANT_TOOLS['shell'])

    def test_new_scopes_default_open_and_narrow(self):
        from agents.models import SubAgent

        user = User.objects.create_user(username='scopes', password='x')
        agent = SubAgent.objects.create(user=user, name='A', prompt='x')
        self.assertEqual(workspace_egress_for(agent), ())
        self.assertIsNone(code_projects_for(agent))
        agent.agent_context = {'workspaceEgress': ['pypi.org'],
                               'codeProjects': [3]}
        self.assertEqual(workspace_egress_for(agent), ('pypi.org',))
        self.assertEqual(code_projects_for(agent), (3,))


class EngineGatingTests(TestCase):
    def test_compute_shell_mission_tools_withheld_with_no_engine(self):
        from agents.agent.runtime import AgentToolbox

        user = User.objects.create_user(username='eng', password='x')
        box = AgentToolbox(
            grants={'compute': True, 'shell': True}, user_id=user.id)
        names = set(box.allowed_names)
        # WORKSPACE_ENGINE=none in test settings: not offered.
        self.assertNotIn('workspace_exec', names)
        self.assertNotIn('ws_run', names)

    def test_mission_caller_is_unattended(self):
        from agents.grants import CALLERS, UNATTENDED_CALLERS

        self.assertIn('mission', CALLERS)
        self.assertIn('mission', UNATTENDED_CALLERS)


class DashboardSpecTests(TestCase):
    def test_render_dashboard_validates_tiles(self):
        out = async_to_sync(
            __import__('chat.tools.dashboards', fromlist=['render_dashboard'])
            .render_dashboard)(
            {'title': '', 'tiles': []}, {'user_id': None})
        import json

        self.assertIn('error', json.loads(out))

    def test_render_dashboard_accepts_kpi_and_table(self):
        import json

        from chat.tools.dashboards import render_dashboard

        out = async_to_sync(render_dashboard)(
            {'title': 'T', 'tiles': [
                {'kind': 'kpi', 'title': 'MRR', 'value': '4.2Cr'},
                {'kind': 'table', 'title': 'Top',
                 'columns': ['a'], 'rows': [['1']]},
            ]}, {'user_id': None})
        payload = json.loads(out)
        self.assertEqual(payload['type'], 'dashboard')
        self.assertEqual(len(payload['tiles']), 2)

    def test_save_dashboard_needs_user(self):
        import json

        from chat.tools.dashboards import save_dashboard

        out = async_to_sync(save_dashboard)(
            {'title': 'T', 'tiles': [{'kind': 'text', 'text': 'hi'}]},
            {'user_id': None})
        self.assertIn('error', json.loads(out))


class NewKnobsMoveToolsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='knobs', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def tearDown(self):
        cache.clear()

    def test_message_send_body_chars_is_configurable(self):
        res = self.client.patch('/api/tools/', {
            'tool_name': 'message_send', 'config': {'bodyChars': 500},
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            limits(self.user.id, 'message_send')['bodyChars'], 500)

    def test_query_sql_max_rows_is_configurable(self):
        res = self.client.patch('/api/tools/', {
            'tool_name': 'query_sql', 'config': {'maxRows': 100},
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(limits(self.user.id, 'query_sql')['maxRows'], 100)

    def test_render_deck_max_slides_is_configurable(self):
        res = self.client.patch('/api/tools/', {
            'tool_name': 'render_deck', 'config': {'maxSlides': 10},
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(limits(self.user.id, 'render_deck')['maxSlides'], 10)

    def test_compute_tools_appear_in_catalogue(self):
        res = self.client.get('/api/tools/')
        self.assertEqual(res.status_code, 200)
        by_name = {t['name']: t
                   for c in res.data['categories'] for t in c['tools']}
        for name in ('workspace_exec', 'start_job', 'ws_run', 'git_push',
                     'mission_status', 'wait_for', 'complete_mission',
                     'report_progress', 'start_mission', 'render_dashboard',
                     'save_dashboard', 'ocr_document',
                     'find_files', 'message_send', 'query_sql'):
            self.assertIn(name, by_name, name)
        cats = {c['key']: c for c in res.data['categories']}
        self.assertIn('workspace_exec',
                      [t['name'] for t in cats['compute']['tools']])
        self.assertIn('ws_run',
                      [t['name'] for t in cats['shell']['tools']])
