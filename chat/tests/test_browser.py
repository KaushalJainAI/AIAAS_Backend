"""
The browser tools (`chat/tools/browser.py`) over `browsing/engine.py`.

The remote browser is faked at the HTTP boundary. What is pinned is the safety
design: the tools do not exist until an engine is configured, steps are data
checked against a closed verb list (never code), an agent may act only on its
listed sites (and empty means read-only), acting is gated where reading is
not, and an internal address is refused before anything leaves the box.
"""
from __future__ import annotations

import json
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings

from browsing import engine
from chat.tools import execute_tool, get_available_tools
from chat.tools.registry import get

REMOTE = dict(BROWSER_ENGINE='remote', BROWSER_REMOTE_URL='https://browser.example', BROWSER_API_TOKEN='t')


class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class _Client:
    def __init__(self, payload, status=200):
        self.sent = []
        self.payload, self.status = payload, status

    async def post(self, url, **kw):
        self.sent.append((url, kw))
        return _Resp(self.payload, self.status)


PAGE = {'data': {'url': 'https://shop.example.com/cart', 'title': 'Cart',
                 'text': 'Your cart has 2 items', 'links': [], 'steps': [{'action': 'click', 'ok': True}]}}


def run(name, args, **ctx):
    return json.loads(async_to_sync(execute_tool)(name, args, {'user_id': 1, **ctx}))


class AvailabilityTests(SimpleTestCase):
    def test_not_offered_without_an_engine(self):
        offered = {t['function']['name'] for t in async_to_sync(get_available_tools)(None)}
        self.assertFalse({'browse_page', 'browser_act'} & offered)

    @override_settings(**REMOTE)
    def test_offered_once_configured(self):
        offered = {t['function']['name'] for t in async_to_sync(get_available_tools)(None)}
        self.assertTrue({'browse_page', 'browser_act'} <= offered)

    def test_reading_is_free_and_acting_is_gated(self):
        self.assertEqual(get('browse_page').effect, 'read')
        self.assertFalse(get('browse_page').sensitive)
        self.assertEqual(get('browser_act').effect, 'irreversible')
        self.assertTrue(get('browser_act').sensitive)

    def test_the_grant_is_withheld_when_no_engine_is_configured(self):
        from agents.agent.runtime import AgentToolbox

        self.assertFalse({'browse_page', 'browser_act'}
                         & AgentToolbox(grants={'browser': True}, user_id=1).allowed_names)
        with override_settings(**REMOTE):
            self.assertTrue({'browse_page', 'browser_act'}
                            <= AgentToolbox(grants={'browser': True}, user_id=1).allowed_names)


@override_settings(**REMOTE)
class ActTests(SimpleTestCase):
    def setUp(self):
        self.client = _Client(PAGE)
        patcher = mock.patch('workflow_backend.httpclient.shared_client', return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        safe = mock.patch('core.safety.net.validate_url_async', return_value=(True, ''))
        safe.start()
        self.addCleanup(safe.stop)

    def test_steps_travel_as_data_to_one_fixed_script(self):
        out = run('browser_act', {'url': 'https://shop.example.com', 'steps': [
            {'action': 'click', 'selector': '#add'}]}, browser_domains=('example.com',))
        self.assertEqual(out['title'], 'Cart')
        _url, sent = self.client.sent[0]
        self.assertEqual(sent['json']['code'], engine._SCRIPT)
        self.assertEqual(sent['json']['context']['steps'],
                         [{'action': 'click', 'selector': '#add', 'text': ''}])

    def test_an_agent_may_act_only_on_its_listed_sites(self):
        out = run('browser_act', {'url': 'https://bank.example.org', 'steps': [
            {'action': 'click', 'selector': '#pay'}]}, browser_domains=('example.com',))
        self.assertIn('may only act on', out['error'])
        self.assertEqual(self.client.sent, [])

    def test_an_empty_list_means_read_only(self):
        out = run('browser_act', {'url': 'https://shop.example.com', 'steps': [
            {'action': 'click', 'selector': '#add'}]}, browser_domains=())
        self.assertIn('none', out['error'])

    def test_subdomains_count_as_the_site(self):
        out = run('browser_act', {'url': 'https://portal.shop.example.com', 'steps': [
            {'action': 'wait', 'selector': 'body'}]}, browser_domains=('example.com',))
        self.assertNotIn('error', out)

    def test_unknown_verbs_and_missing_fields_are_refused(self):
        for step, message in (({'action': 'eval', 'selector': 'x'}, 'action must be'),
                              ({'action': 'type', 'selector': '#q'}, 'needs `text`'),
                              ({'action': 'click'}, 'CSS selector')):
            with self.subTest(step=step):
                out = run('browser_act', {'url': 'https://shop.example.com', 'steps': [step]})
                self.assertIn(message, out['error'])

    def test_reading_works_without_any_domain_list(self):
        out = run('browse_page', {'url': 'https://anything.example.net'}, browser_domains=())
        self.assertEqual(out['text'], 'Your cart has 2 items')


@override_settings(**REMOTE)
class GuardTests(SimpleTestCase):
    def test_an_internal_address_is_refused_before_anything_is_sent(self):
        client = _Client(PAGE)
        with mock.patch('workflow_backend.httpclient.shared_client', return_value=client):
            out = run('browse_page', {'url': 'http://169.254.169.254/latest/meta-data'})
        self.assertIn('error', out)
        self.assertEqual(client.sent, [])

    def test_a_rate_limit_is_reported_plainly(self):
        client = _Client({}, status=429)
        with mock.patch('workflow_backend.httpclient.shared_client', return_value=client), \
             mock.patch('core.safety.net.validate_url_async', return_value=(True, '')):
            out = run('browse_page', {'url': 'https://example.com'})
        self.assertIn('rate limited', out['error'])
