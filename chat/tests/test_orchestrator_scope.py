"""
Chat is the orchestrator and holds only basic tools (2026-09-25).

Decentralised orchestration: critical access lives in subagents the user
configures explicitly. Chat reads, plans and delegates; it does not write
files, send mail, publish, render or spend. Pinned: the orchestrator's
toolbox is reads + infrastructure + delegation/authoring/memory/missions,
and a critical call the model names anyway is refused at dispatch with a
message pointing at delegation.
"""
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from chat.tools import (
    CHAT_ORCHESTRATOR_EXTRA,
    ORCHESTRATOR_REFUSAL,
    chat_orchestrator_allowed,
    execute_chat_tool,
    execute_tool,
    get_available_tools,
)


class ScopeTests(SimpleTestCase):
    def test_reads_are_allowed(self):
        for name in ('read_file', 'list_files', 'find_files', 'web_search',
                     'read_url', 'search_agents', 'get_agent_run',
                     'gmail_get_message', 'ask_user', 'update_todos'):
            with self.subTest(name=name):
                self.assertTrue(chat_orchestrator_allowed(name), name)

    def test_critical_actions_are_not(self):
        for name in ('write_file', 'edit_file', 'delete_file', 'make_directory',
                     'render_deck', 'render_workbook', 'render_document',
                     'render_pdf', 'edit_workbook', 'generate_image',
                     'gmail_send_message', 'gmail_create_draft',
                     'message_send', 'publish_page', 'browser_act',
                     'execute_sql', 'call_api', 'ws_run', 'git_push',
                     'request_signature', 'text_to_speech'):
            with self.subTest(name=name):
                self.assertFalse(chat_orchestrator_allowed(name), name)

    def test_delegation_and_authoring_stay(self):
        for name in ('run_agent', 'invoke_subagent', 'answer_subagent',
                     'search_agents', 'get_agent_run', 'create_agent',
                     'update_agent', 'start_tasks', 'wait_tasks',
                     'remember_about_user', 'forget_about_user',
                     'start_mission'):
            with self.subTest(name=name):
                self.assertTrue(chat_orchestrator_allowed(name), name)

    def test_an_unknown_name_fails_closed(self):
        self.assertFalse(chat_orchestrator_allowed('mcp__7__send_email_ab12cd34'))
        self.assertFalse(chat_orchestrator_allowed('nonsense_tool'))


class DispatchTests(SimpleTestCase):
    def test_a_critical_call_is_refused_with_the_delegation_message(self):
        out = async_to_sync(execute_chat_tool)(
            'write_file', {'path': 'a.md'}, {'user_id': 1})
        self.assertIn('Not available in this chat turn', out)
        self.assertIn('search_agents', out)
        self.assertIn(ORCHESTRATOR_REFUSAL[:40], out)

    def test_an_mcp_write_is_refused_too(self):
        out = async_to_sync(execute_chat_tool)(
            'mcp__7__send_email_ab12cd34', {}, {'user_id': 1})
        self.assertIn('Not available in this chat turn', out)


class CatalogueTests(SimpleTestCase):
    def test_the_offered_catalogue_has_no_critical_builtin(self):
        tools = async_to_sync(get_available_tools)(
            None, memory_enabled=True, file_scope=object())
        names = {t['function']['name'] for t in tools}
        self.assertNotIn('write_file', names)
        self.assertNotIn('gmail_send_message', names)
        self.assertIn('read_file', names)
        self.assertIn('search_agents', names)


class SharedDispatcherTests(SimpleTestCase):
    """`execute_tool` is also the end of `AgentToolbox.dispatch`.

    The orchestrator's scope must not live there, or every subagent loses the
    writes, sends and renders it was granted — the tools chat now delegates to
    it. The scope is chat's door alone.
    """

    def test_the_shared_dispatcher_does_not_refuse_a_write(self):
        from unittest import mock

        with mock.patch('chat.tools.disabled_tools_for', return_value=frozenset()):
            with mock.patch('chat.tools.get') as get:
                get.return_value = mock.Mock(
                    connector=None, run=mock.AsyncMock(return_value='wrote a.md'))
                out = async_to_sync(execute_tool)(
                    'write_file', {'path': 'a.md'}, {'user_id': 1})
        self.assertEqual(out, 'wrote a.md')

    def test_the_agent_toolbox_dispatches_through_the_unscoped_door(self):
        import inspect

        from agents.agent.runtime import AgentToolbox

        src = inspect.getsource(AgentToolbox.dispatch)
        self.assertIn('execute_tool(', src)
        self.assertNotIn('execute_chat_tool', src)
