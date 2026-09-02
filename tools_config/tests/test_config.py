"""
The tool library overlay: what a user may change, and that changing it is felt.

The failure this file exists to prevent is a switch that looks like it worked.
A toggle that writes a row nothing reads is worse than no toggle at all - the
user believes a capability is off and it is not - so most of these tests assert
on the *runtime's* view (what is offered, what a dispatch does), not on the row.
"""
from __future__ import annotations

import re
from pathlib import Path

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from chat.tools import disabled_tools_for, execute_tool, get_available_tools
from tools_config.models import ToolConfig
from tools_config.overlay import limits
from tools_config.settings_schema import LOCKED_TOOLS, TOOL_SETTINGS

User = get_user_model()


def _names(descriptors) -> set[str]:
    return {d.get('function', {}).get('name') for d in descriptors}


class OverlayBaseTest(TestCase):
    def setUp(self):
        cache.clear()  # the overlay is cached per user; tests must not share one
        self.user = User.objects.create_user(username='alice', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def tearDown(self):
        cache.clear()


class CatalogueTests(OverlayBaseTest):
    def test_fresh_account_has_no_rows_and_everything_on(self):
        res = self.client.get('/api/tools/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(ToolConfig.objects.count(), 0)
        tools = [t for c in res.data['categories'] for t in c['tools']]
        self.assertTrue(tools)
        self.assertTrue(all(t['enabled'] for t in tools if not t['unserved']))
        self.assertFalse(any(t['customized'] for t in tools))

    def test_catalogue_carries_the_knob_schema_not_just_values(self):
        res = self.client.get('/api/tools/')
        by_name = {t['name']: t for c in res.data['categories'] for t in c['tools']}
        read_url = by_name['read_url']
        self.assertEqual([s['key'] for s in read_url['settings']], ['charLimit'])
        self.assertEqual(read_url['config']['charLimit'],
                         read_url['settings'][0]['default'])
        # A tool with no knobs says so with an empty list, not by omission.
        self.assertEqual(by_name['get_current_time']['settings'], [])


class KillSwitchTests(OverlayBaseTest):
    def test_disabling_a_tool_withdraws_it_from_chat(self):
        before = _names(async_to_sync(get_available_tools)(self.user.id))
        self.assertIn('video_search', before)

        res = self.client.patch(
            '/api/tools/', {'tool_name': 'video_search', 'enabled': False},
            format='json',
        )
        self.assertEqual(res.status_code, 200)

        after = _names(async_to_sync(get_available_tools)(self.user.id))
        self.assertNotIn('video_search', after)
        self.assertIn('web_search', after)

    def test_a_disabled_tool_is_refused_at_dispatch_not_only_hidden(self):
        # The model can name a tool it saw earlier in the transcript, so hiding
        # it from the listing is not the same as switching it off.
        self.client.patch('/api/tools/',
                          {'tool_name': 'video_search', 'enabled': False},
                          format='json')
        out = async_to_sync(execute_tool)(
            'video_search', {'query': 'anything'}, {'user_id': self.user.id},
        )
        self.assertIn('switched off', out)

    def test_disabling_withdraws_it_from_an_agent_toolbox_too(self):
        from agents.agent.runtime import AgentToolbox

        box = AgentToolbox(grants={'webSearch': True}, user_id=self.user.id)
        self.assertIn('video_search', _names(async_to_sync(box.descriptors)()))

        self.client.patch('/api/tools/',
                          {'tool_name': 'video_search', 'enabled': False},
                          format='json')
        self.assertNotIn('video_search', _names(async_to_sync(box.descriptors)()))

    def test_another_users_switch_does_not_reach_this_one(self):
        bob = User.objects.create_user(username='bob', password='x')
        ToolConfig.objects.create(user=bob, tool_name='video_search', enabled=False)
        self.assertEqual(async_to_sync(disabled_tools_for)(self.user.id), frozenset())
        self.assertEqual(async_to_sync(disabled_tools_for)(bob.id),
                         frozenset({'video_search'}))

    def test_locked_tools_cannot_be_switched_off(self):
        for name in sorted(LOCKED_TOOLS):
            res = self.client.patch('/api/tools/',
                                    {'tool_name': name, 'enabled': False},
                                    format='json')
            self.assertEqual(res.status_code, 400, name)
        self.assertEqual(ToolConfig.objects.count(), 0)

    def test_an_unknown_tool_is_refused_rather_than_stored(self):
        res = self.client.patch('/api/tools/',
                                {'tool_name': 'web_serch', 'enabled': False},
                                format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(ToolConfig.objects.count(), 0)

    def test_a_category_switch_is_one_request(self):
        res = self.client.patch('/api/tools/', {'tools': {
            'web_search': {'enabled': False},
            'image_search': {'enabled': False},
            'video_search': {'enabled': False},
            'deep_research': {'enabled': False},
        }}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(ToolConfig.objects.filter(enabled=False).count(), 4)
        self.assertNotIn(
            'web_search', _names(async_to_sync(get_available_tools)(self.user.id)),
        )


class ConfigValueTests(OverlayBaseTest):
    def test_a_value_is_clamped_to_its_declared_range(self):
        res = self.client.patch('/api/tools/', {
            'tool_name': 'web_search', 'config': {'maxResults': 9_999},
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(limits(self.user.id, 'web_search')['maxResults'], 25)

    def test_an_undeclared_key_is_dropped_not_stored(self):
        self.client.patch('/api/tools/', {
            'tool_name': 'web_search',
            'config': {'maxResults': 8, 'nonsense': 3},
        }, format='json')
        row = ToolConfig.objects.get(user=self.user, tool_name='web_search')
        self.assertEqual(row.config, {'maxResults': 8})

    def test_returning_to_the_default_deletes_the_row(self):
        default = TOOL_SETTINGS['web_search'][0].default
        self.client.patch('/api/tools/',
                          {'tool_name': 'web_search', 'config': {'maxResults': 8}},
                          format='json')
        self.assertEqual(ToolConfig.objects.count(), 1)
        self.client.patch(
            '/api/tools/',
            {'tool_name': 'web_search', 'config': {'maxResults': default}},
            format='json',
        )
        # Absent *is* the default: two representations of one state is how a
        # changed default silently fails to reach anyone who opened the panel.
        self.assertEqual(ToolConfig.objects.count(), 0)

    def test_a_disabled_tool_with_a_custom_value_keeps_its_row(self):
        self.client.patch('/api/tools/', {'tools': {
            'web_search': {'enabled': False, 'config': {'maxResults': 8}},
        }}, format='json')
        row = ToolConfig.objects.get(user=self.user, tool_name='web_search')
        self.assertFalse(row.enabled)
        self.assertEqual(row.config, {'maxResults': 8})


class SchemaIsWiredTests(TestCase):
    """Every declared knob is read by the tool it belongs to.

    A knob the UI renders and no tool reads is the same lie as a switch that
    writes an unread row, and it is the easy one to introduce: adding a
    `Setting` costs nothing, wiring it is a separate edit.
    """

    def test_every_declared_setting_is_read_somewhere_in_chat_tools(self):
        source = '\n'.join(
            p.read_text(encoding='utf-8')
            for p in (Path(__file__).resolve().parents[2] / 'chat' / 'tools').glob('*.py')
        )
        for tool_name, settings in TOOL_SETTINGS.items():
            for setting in settings:
                pattern = (
                    r'alimit\(\s*context\s*,\s*[\'"]' + re.escape(tool_name)
                    + r'[\'"]\s*,\s*[\'"]' + re.escape(setting.key) + r'[\'"]'
                )
                self.assertRegex(
                    source, pattern,
                    msg=f'{tool_name}.{setting.key} is offered but never read',
                )

    def test_locked_tools_exist(self):
        from chat.tools.registry import all_tools

        registered = {t.name for t in all_tools()}
        self.assertEqual(LOCKED_TOOLS - registered, set())
