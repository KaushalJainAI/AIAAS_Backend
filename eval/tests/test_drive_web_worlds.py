"""
E-4: simulated Drive/Sheets/Docs and a frozen web.

Worlds with `drive`/`web` surfaces answer those tools from fixtures — sheet
writes land in the simulated tabs, unknown queries get "no results", nothing
reaches any real service — and `env_cell`/`env_file`/`cited(url)` grade what
the run did and read. Frozen means a research case scores the same a week
apart: determinism is asserted, not assumed.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents.models import SubAgent
from eval import environment as envmod
from eval.models import EvalCase, EvalSuite, EvalWorld
from eval.sim.drive import DriveSim
from eval.sim.web import WebSim


DRIVE_FIXTURES = {
    'files': [
        {'file_id': 'f1', 'name': 'Budget Q3', 'mime_type': 'document',
         'content': 'Q3 budget is 412300.'},
        {'file_id': 'ss1', 'name': 'Stock', 'mime_type': 'spreadsheet',
         'content': ''},
    ],
    'sheets': {'ss1': {'tabs': {'Sheet1': [
        ['sku', 'qty'], ['a-1', '3'], ['b-2', '7']]}}},
}

WEB_FIXTURES = {
    'pages': [{'url': 'https://acme.test/pricing', 'title': 'Acme pricing',
               'text': '# Pricing\nThe Pro plan costs $49 per seat per month.'}],
    'results': {'acme pricing': ['https://acme.test/pricing']},
}


class DriveSimTests(SimpleTestCase):
    def setUp(self):
        self.sim = DriveSim(DRIVE_FIXTURES)

    def test_search_and_recent_and_metadata(self):
        out = json.loads(self.sim.run('drive_search_files', {'query': 'budget'}))
        self.assertEqual(out['type'], 'drive_files')
        self.assertEqual(out['files'][0]['file_id'], 'f1')
        self.assertEqual(
            self.sim.run('drive_search_files', {}),
            "Error: 'query' is required.")
        recent = json.loads(self.sim.run('drive_list_recent_files', {}))
        self.assertEqual(recent['files'][0]['file_id'], 'ss1')
        meta = json.loads(self.sim.run(
            'drive_get_file_metadata', {'file_id': 'f1'}))
        self.assertEqual(meta['name'], 'Budget Q3')
        self.assertEqual(
            json.loads(self.sim.run(
                'drive_get_file_metadata', {'file_id': 'nope'}))['code'],
            'not_found')

    def test_read_content_shapes(self):
        doc = json.loads(self.sim.run(
            'drive_read_file_content', {'file_id': 'f1'}))
        self.assertEqual(doc['type'], 'drive_file_content')
        self.assertIn('412300', doc['content'])
        self.assertIsNone(doc['next_offset'])
        sheet = json.loads(self.sim.run(
            'drive_read_file_content', {'file_id': 'ss1'}))
        self.assertIn('sku,qty', sheet['content'])
        self.assertIn('first sheet', sheet['note'])

    def test_create_file(self):
        created = json.loads(self.sim.run('drive_create_file', {
            'name': 'Summary', 'content': 'done', 'as_google_doc': True}))
        self.assertEqual(created['status'], 'success')
        self.assertTrue(created['file_id'].startswith('sim-file-'))
        read = json.loads(self.sim.run(
            'docs_read', {'document_id': created['file_id']}))
        self.assertEqual(read['text'], 'done')
        self.assertEqual(
            self.sim.run('drive_create_file', {'content': 'x'}),
            "Error: 'name' is required.")

    def test_sheets_get_and_update(self):
        tabs = json.loads(self.sim.run('sheets_get_values', {
            'spreadsheet_id': 'ss1'}))
        self.assertEqual(tabs['tabs'], ['Sheet1'])
        self.assertEqual(tabs['values'][0], ['sku', 'qty'])
        cell = json.loads(self.sim.run('sheets_get_values', {
            'spreadsheet_id': 'ss1', 'range': 'Sheet1!B2'}))
        self.assertEqual(cell['values'], [['3']])
        self.assertEqual(
            json.loads(self.sim.run('sheets_get_values', {
                'spreadsheet_id': 'ss1', 'range': 'Missing!A1'}))['code'],
            'not_found')
        updated = json.loads(self.sim.run('sheets_update_values', {
            'spreadsheet_id': 'ss1', 'range': 'Sheet1!B2', 'values': [['30']]}))
        self.assertEqual(updated['updated_cells'], 1)
        cell = json.loads(self.sim.run('sheets_get_values', {
            'spreadsheet_id': 'ss1', 'range': 'Sheet1!B2'}))
        self.assertEqual(cell['values'], [['30']])
        self.assertEqual(
            self.sim.run('sheets_update_values', {'spreadsheet_id': 'ss1'}),
            "Error: 'spreadsheet_id', 'range' and 'values' are required.")

    def test_docs_create_and_append(self):
        created = json.loads(self.sim.run('docs_create', {'title': 'Minutes'}))
        self.assertTrue(created['id'].startswith('sim-doc-'))
        appended = json.loads(self.sim.run('docs_append', {
            'document_id': created['id'], 'text': 'Agreed.\n\nNext: ship.'}))
        self.assertIn('Appended', appended['rendered'])
        read = json.loads(self.sim.run(
            'docs_read', {'document_id': created['id']}))
        self.assertIn('Agreed.', read['text'])
        self.assertEqual(
            json.loads(self.sim.run('docs_create', {'title': ''}))['code'],
            'tool_error')

    def test_snapshot_changes_and_expected(self):
        self.sim.run('sheets_update_values', {
            'spreadsheet_id': 'ss1', 'range': 'Sheet1!B2', 'values': [['30']]})
        snap = self.sim.snapshot()
        self.assertEqual(snap['sheets']['ss1']['tabs']['Sheet1'][1], ['a-1', '30'])
        delta = self.sim.changes()
        self.assertEqual(delta['cells_updated'][0]['updated'], 1)
        fresh = DriveSim(DRIVE_FIXTURES)
        fresh.apply_expected({'drive_cells': [
            {'spreadsheet_id': 'ss1', 'range': 'Sheet1!B3',
             'values': [['70']]}]})
        self.assertEqual(
            fresh.snapshot()['sheets']['ss1']['tabs']['Sheet1'][2], ['b-2', '70'])

    def test_sims_never_raise(self):
        self.assertTrue(self.sim.run('nope', {}).startswith('Error:'))


class WebSimTests(SimpleTestCase):
    def setUp(self):
        self.sim = WebSim(WEB_FIXTURES)

    def test_search_hit_and_miss(self):
        hit = json.loads(self.sim.run('web_search', {'query': 'Acme pricing'}))
        self.assertEqual(hit['type'], 'search_results')
        self.assertIn('https://acme.test/pricing', hit['text'])
        self.assertEqual(hit['sources'][0]['title'], 'Acme pricing')
        miss = json.loads(self.sim.run('web_search', {'query': 'weather'}))
        self.assertEqual(miss['sources'], [])
        self.assertIn('No results', miss['text'])
        self.assertEqual(
            self.sim.run('web_search', {'query': ''}),
            "Error: 'query' is required.")

    def test_read_and_scrape(self):
        read = json.loads(self.sim.run(
            'read_url', {'url': 'https://acme.test/pricing'}))
        self.assertIn('$49', read['content'])
        self.assertEqual(
            json.loads(self.sim.run('read_url', {'url': 'https://x.test/'})
                       )['error'][:2], 'No')
        scraped = json.loads(self.sim.run('scrape_webpage', {
            'url': 'https://acme.test/pricing', 'extract': ['headings', 'text']}))
        self.assertEqual(scraped['status'], 'success')
        self.assertEqual(scraped['headings'][0]['text'], 'Pricing')
        self.assertNotIn('links', scraped)
        self.assertEqual(
            json.loads(self.sim.run(
                'scrape_webpage', {'url': 'https://x.test/'})
                       )['status'], 'error')

    def test_frozen_and_pure(self):
        first = self.sim.run('web_search', {'query': 'acme pricing'})
        second = self.sim.run('web_search', {'query': 'acme pricing'})
        self.assertEqual(first, second)
        before = json.dumps(WEB_FIXTURES, sort_keys=True)
        self.sim.run('web_search', {'query': 'acme pricing'})
        self.sim.run('read_url', {'url': 'https://acme.test/pricing'})
        self.assertEqual(json.dumps(WEB_FIXTURES, sort_keys=True), before)
        snap = self.sim.snapshot()
        self.assertEqual(snap['queries'], ['acme pricing'] * 3)
        self.assertEqual(snap['reads'], ['https://acme.test/pricing'])


class DriveWebCoverageTests(TestCase):
    def test_drive_docs_and_web_tools_all_simulated(self):
        from chat.tools import AVAILABLE_TOOLS
        from chat.tools.registry import connector_of
        from eval.sim.drive import DriveSim
        from eval.sim.web import WebSim

        by_connector: dict[str, set[str]] = {}
        for tool in AVAILABLE_TOOLS:
            name = tool.get('function', {}).get('name')
            slug = connector_of(name) if name else None
            if slug in ('google-drive', 'google-sheets', 'google-docs'):
                by_connector.setdefault(slug, set()).add(name)
        simmed = set(DriveSim.TOOLS)
        for slug, names in by_connector.items():
            self.assertEqual(simmed & names, names, slug)
        for name in ('web_search', 'read_url', 'scrape_webpage'):
            self.assertIn(name, WebSim.TOOLS)

    def test_deep_research_stays_withheld(self):
        from eval import environment as envmod
        from types import SimpleNamespace
        env = envmod.EvalEnvironment(
            user=None, agent=None, suite=None,
            world=SimpleNamespace(
                fixtures={'files': {}, 'drive': DRIVE_FIXTURES,
                          'web': WEB_FIXTURES},
                surfaces={'files': True, 'drive': True, 'web': True},
                version=1))
        env.sims = env._build_sims()
        self.assertTrue(env.simulates('web_search'))
        self.assertTrue(env.simulates('sheets_update_values'))
        self.assertFalse(env.simulates('deep_research'))
        self.assertFalse(env.simulates('download_file'))
        allowed = {'web_search', 'read_url', 'deep_research', 'download_file',
                   'drive_search_files', 'read_file'}
        self.assertEqual(env.withheld_names(allowed),
                         {'deep_research', 'download_file'})


class DriveWebGraderTests(SimpleTestCase):
    def _grade(self, spec, **ctx):
        from eval import graders
        return async_to_sync(graders.grade_all)(
            [spec], graders.GradeContext(**ctx))[0][0]

    ENV = {'drive': {
        'files': [{'file_id': 'f1', 'name': 'Budget Q3',
                   'content': 'Q3 budget is 412300.'}],
        'sheets': {'ss1': {'tabs': {'Sheet1': [
            ['sku', 'qty'], ['a-1', '3'], ['b-2', '7']]}}}},
        'web': {'pages': [{'url': 'https://acme.test/pricing',
                           'title': 'Acme pricing'}]}}

    def test_env_cell(self):
        self.assertTrue(self._grade(
            {'type': 'env_cell', 'spreadsheet_id': 'ss1',
             'cell': 'B2', 'equals': 3}, env=self.ENV).passed)
        self.assertTrue(self._grade(
            {'type': 'env_cell', 'spreadsheet_id': 'ss1',
             'match': {'sku': 'b-2'}, 'column': 'qty', 'equals': '7'},
            env=self.ENV).passed)
        self.assertTrue(self._grade(
            {'type': 'env_cell', 'spreadsheet_id': 'ss1',
             'match': {'sku': 'b-2'}, 'column': 'B', 'equals': 7},
            env=self.ENV).passed)
        self.assertFalse(self._grade(
            {'type': 'env_cell', 'spreadsheet_id': 'ss1',
             'cell': 'B2', 'equals': 4}, env=self.ENV).passed)
        self.assertFalse(self._grade(
            {'type': 'env_cell', 'spreadsheet_id': 'nope',
             'cell': 'A1', 'equals': 1}, env=self.ENV).passed)

    def test_env_file(self):
        self.assertTrue(self._grade(
            {'type': 'env_file', 'name': 'budget'}, env=self.ENV).passed)
        self.assertTrue(self._grade(
            {'type': 'env_file', 'name': 'budget', 'contains': '412300'},
            env=self.ENV).passed)
        self.assertFalse(self._grade(
            {'type': 'env_file', 'name': 'budget', 'contains': 'refund'},
            env=self.ENV).passed)
        self.assertFalse(self._grade(
            {'type': 'env_file', 'name': 'missing'}, env=self.ENV).passed)
        self.assertFalse(self._grade(
            {'type': 'env_file'}, env=self.ENV).passed)

    def test_cited_url(self):
        trace = [{'tool': 'read_url',
                  'args': {'url': 'https://acme.test/pricing/'}}]
        self.assertTrue(self._grade(
            {'type': 'cited', 'doc': 'https://acme.test/pricing'},
            env=self.ENV, tool_trace=trace).passed)
        self.assertFalse(self._grade(
            {'type': 'cited', 'doc': 'https://acme.test/pricing'},
            env=self.ENV).passed)
        self.assertFalse(self._grade(
            {'type': 'cited', 'doc': 'https://other.test/'},
            env=self.ENV, tool_trace=trace).passed)


class NoRealDriveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('driver', 'd@example.com', 'pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Researcher',
            tool_grants={'mcp': True, 'webSearch': True, 'scrape': True,
                         'fileOps': True},
            sandbox={'fileAccess': 'scoped'},
            guardrails={'autonomy': 'ask'},
            agent_context={'connectors': []},
            prompt='Research.')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='Research', subagent=self.agent,
            supervision='failures')
        self.world = EvalWorld.objects.create(
            suite=self.suite, version=1, status='accepted',
            brief='Acme research.',
            surfaces={'files': True, 'drive': True, 'web': True},
            fixtures={'files': {'notes.md': 'Research acme.\n'},
                      'drive': dict(DRIVE_FIXTURES),
                      'web': dict(WEB_FIXTURES)},
            facts=[{'key': 'price', 'value': '$49',
                    'statement': 'Pro costs $49 per seat.'}])

    def _box(self):
        from agents.agent.runtime import AgentToolbox
        env = envmod.for_attempt(self.user, self.agent, self.suite, None)
        env.prepare(None, None, 'r1')
        box = AgentToolbox.for_agent(
            self.agent, self.user.id, file_scope=env.attempt_scope(),
            session_key='t', environment=env)
        return box, env

    def test_drive_and_web_offered_without_connections(self):
        box, env = self._box()
        names = {d['function']['name']
                 for d in async_to_sync(box.descriptors)()}
        for name in ('drive_search_files', 'sheets_get_values',
                     'sheets_update_values', 'docs_read', 'web_search',
                     'read_url', 'scrape_webpage'):
            self.assertIn(name, names, name)
        for name in ('deep_research', 'download_file'):
            self.assertNotIn(name, names, name)

    def test_writes_are_simulated_and_graded(self):
        from agents.agent.runtime import AgentRun
        from eval import runner
        box, env = self._box()
        with patch('chat.tools.google.client.get_json',
                   side_effect=AssertionError('real Drive touched')), \
             patch('chat.tools.google.client.send_json',
                   side_effect=AssertionError('real Drive touched')), \
             patch('chat.tools.google.client.download',
                   side_effect=AssertionError('real Drive touched')):
            updated = json.loads(async_to_sync(box.dispatch)(
                'sheets_update_values',
                {'spreadsheet_id': 'ss1', 'range': 'Sheet1!B2',
                 'values': [['30']]},
                {'user_id': self.user.id}))
            self.assertEqual(updated['updated_cells'], 1)
            read = json.loads(async_to_sync(box.dispatch)(
                'web_search', {'query': 'acme pricing'},
                {'user_id': self.user.id}))
            self.assertIn('acme.test/pricing', read['text'])
        snap = env.snapshot_env()
        self.assertEqual(
            snap['drive']['sheets']['ss1']['tabs']['Sheet1'][1], ['a-1', '30'])
        self.assertEqual(env.changes({})['drive']['cells_updated'][0]['updated'], 1)

    def test_sweep_grades_cells_and_citations(self):
        from unittest.mock import patch as _patch

        from agents.agent.runtime import AgentRun
        from eval import runner

        case = EvalCase.objects.create(
            suite=self.suite, goal='Set a-1 qty to 30 and cite pricing.',
            is_active=True, world_version=1,
            reference='Correct answer: done.',
            graders=[{'type': 'env_cell', 'spreadsheet_id': 'ss1',
                      'match': {'sku': 'a-1'}, 'column': 'qty', 'equals': 30},
                     {'type': 'cited',
                      'doc': 'https://acme.test/pricing'}])

        async def fake(agent, goal, **kwargs):
            env = kwargs['environment']
            await env.run_simulated(
                'sheets_update_values',
                {'spreadsheet_id': 'ss1', 'range': 'Sheet1!B2',
                 'values': [['30']]}, {})
            await env.run_simulated(
                'read_url', {'url': 'https://acme.test/pricing'}, {})
            trace = [{'tool': 'sheets_update_values',
                      'args': {'spreadsheet_id': 'ss1'}, 'iteration': 1},
                     {'tool': 'read_url',
                      'args': {'url': 'https://acme.test/pricing'},
                      'iteration': 2}]
            return AgentRun(
                execution_id='00000000-0000-0000-0000-000000000003',
                answer='Set to 30; pricing is $49.', thinking='',
                tool_trace=trace, tokens=10, awaiting_approval=False,
                unserved_grants=(), duration_ms=50)

        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user, '')
        with _patch('agents.agent.runtime.run_agent', fake):
            async_to_sync(runner.sweep)(run, self.suite, self.agent, self.user)
        run.refresh_from_db()
        result = run.results.get()
        self.assertTrue(result.auto_passed, result.grades)
        self.assertEqual(
            result.env_changes['drive']['cells_updated'][0]['updated'], 1)


class DriveWebValidationTests(SimpleTestCase):
    def test_sheet_without_a_file_and_result_without_a_page(self):
        errors = envmod.validate_world(
            {'files': True, 'drive': True},
            {'files': {'a.md': 'x'},
             'drive': {'files': [], 'sheets': {'ghost': {'tabs': {}}}}},
            [{'key': 'k', 'value': 'x', 'statement': 'x'}])
        self.assertTrue(any('no drive file' in e for e in errors))
        errors = envmod.validate_world(
            {'files': True, 'web': True},
            {'files': {'a.md': 'x'},
             'web': {'pages': [{'url': 'https://a.test/', 'title': 'A',
                                'text': 'x'}],
                     'results': {'q': ['https://ghost.test/']}}},
            [{'key': 'k', 'value': 'x', 'statement': 'x'}])
        self.assertTrue(any('names no page' in e for e in errors))

    def test_drive_and_web_surfaces_for_grants(self):
        from types import SimpleNamespace
        from eval.generator import surfaces_for_agent
        agent = SimpleNamespace(
            tool_grants={'mcp': True, 'webSearch': True, 'fileOps': True},
            sandbox={'fileAccess': 'scoped'})
        surfaces = surfaces_for_agent(agent)
        self.assertTrue(surfaces['drive'])
        self.assertTrue(surfaces['web'])

    def test_drive_case_proves(self):
        from eval.generator import clean_env_case, prove_case
        raw = dict(
            name='Stock?', category='normal', goal='Set a-1 qty to 30.',
            input_data={}, facts_used=[], expected='Set.',
            expect_state={'drive_cells': [
                {'spreadsheet_id': 'ss1', 'range': 'Sheet1!B2',
                 'values': [['30']]}]},
            graders=[{'type': 'env_cell', 'spreadsheet_id': 'ss1',
                      'match': {'sku': 'a-1'}, 'column': 'qty',
                      'equals': 30}])
        case, why = clean_env_case(
            raw, {'sheets_get_values', 'sheets_update_values'}, [], ())
        self.assertIsNotNone(case, why)
        fixtures = {'files': {}, 'drive': dict(DRIVE_FIXTURES)}
        ok, reason = async_to_sync(prove_case)(fixtures, case, ())
        self.assertTrue(ok, reason)
