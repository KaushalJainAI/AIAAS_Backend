"""
The tools added on 2026-09-20: download_file, render_pdf, edit_workbook,
render_diagram, extract_data, notify_user — and the per-agent tool scope that
keeps a growing toolbox from landing on every agent.

Each is pinned on the property that made it worth building: a download lands
as a real file the other tools accept, a PDF carries the document's words, an
edit keeps what it did not touch, a diagram is laid out by us and not by the
model, and a notification cannot become a log.
"""
from __future__ import annotations

import io
import json
import shutil
import tempfile
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from chat.tools import execute_tool
from chat.tools.office import diagram, document, edit, pdf, workbook
from chat.tools.office.spec import SpecError
from chat.tools.registry import get
from inference import vfs

User = get_user_model()

DOC_SPEC = {
    'title': 'Q3 review', 'subtitle': 'For the board',
    'blocks': [
        {'type': 'heading', 'text': 'Summary', 'level': 1},
        {'type': 'paragraph', 'text': 'Revenue grew **38%** in <Q3>.'},
        {'type': 'bullets', 'items': ['Pune office opened']},
        {'type': 'table', 'columns': ['Region', 'Revenue'], 'rows': [['North', '120']],
         'caption': 'Table 1'},
        {'type': 'chart', 'chart': {'kind': 'column', 'title': 'Revenue', 'series': [
            {'name': 'R', 'points': [{'x': 'Q1', 'y': 3.1}]}]}},
        {'type': 'page_break'},
    ],
}


class ScopedToolTestCase(TestCase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.scope = vfs.chat_scope(self.user)

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)

    def call(self, name, args, **extra):
        ctx = {'user_id': self.user.id, 'file_scope': self.scope, **extra}
        return json.loads(async_to_sync(execute_tool)(name, args, ctx))


# ─────────────────────────────────────────────────────────── render_pdf

class PdfTests(ScopedToolTestCase):
    def test_a_pdf_carries_the_documents_words(self):
        out = self.call('render_pdf', {'path': 'review.pdf', **DOC_SPEC})
        self.assertEqual(out['path'], '/Chat/review.pdf')
        from inference.models import Document
        from pypdf import PdfReader

        with Document.objects.get(id=out['document_id']).file.open('rb') as handle:
            text = '\n'.join(page.extract_text() or '' for page in PdfReader(handle).pages)
        for fragment in ('Q3 review', 'Summary', 'Revenue grew', 'Pune office', 'Table 1'):
            self.assertIn(fragment, text)
        self.assertEqual(out['charts_as_tables'], 1)

    def test_it_shares_render_documents_spec_and_refusals(self):
        out = self.call('render_pdf', {'title': 'x', 'blocks': [{'type': 'hologram'}]})
        self.assertIn('Block 1', out['error'])

    def test_markup_in_the_text_is_escaped_not_rendered(self):
        # ReportLab reads a small HTML subset, so a document quoting a page
        # must not be able to inject tags into the PDF.
        body = pdf._rich('a <b>bold</b> & **real** claim')
        self.assertIn('&lt;b&gt;', body)
        self.assertIn('<b>real</b>', body)


# ─────────────────────────────────────────────────────── edit_workbook

class EditWorkbookTests(ScopedToolTestCase):
    def a_workbook(self, **extra):
        return self.call('render_workbook', {'path': 'tracker.xlsx', 'sheets': [{
            'name': 'Log', 'columns': [{'header': 'Day'}, {'header': 'Units', 'type': 'integer'}],
            'rows': [['Mon', 3]], **extra}]})

    def test_appended_rows_land_after_the_last_one(self):
        self.a_workbook()
        out = self.call('edit_workbook', {'path': '/Chat/tracker.xlsx',
                                          'append_rows': [['Tue', 5], ['Wed', 8]]})
        self.assertEqual((out['appended'], out['first_new_row']), (2, 3))
        book = self.open('/Chat/tracker.xlsx')
        self.assertEqual([c.value for c in book['Log'][4]], ['Wed', 8])

    def test_what_it_does_not_name_survives(self):
        self.a_workbook(chart={'kind': 'column', 'title': 'Units', 'x': 'Day', 'y': ['Units']})
        self.call('edit_workbook', {'path': '/Chat/tracker.xlsx', 'append_rows': [['Tue', 5]]})
        sheet = self.open('/Chat/tracker.xlsx')['Log']
        self.assertEqual(len(sheet._charts), 1)          # the chart is still there
        self.assertEqual(sheet['A1'].value, 'Day')       # and so is the header

    def test_cells_can_be_set_including_formulas(self):
        self.a_workbook()
        self.call('edit_workbook', {'path': '/Chat/tracker.xlsx',
                                    'set_cells': [{'cell': 'C1', 'value': 'Total'},
                                                  {'cell': 'C2', 'value': '=SUM(B2:B2)'}]})
        sheet = self.open('/Chat/tracker.xlsx')['Log']
        self.assertEqual((sheet['C1'].value, sheet['C2'].value), ('Total', '=SUM(B2:B2)'))

    def test_refusals(self):
        self.a_workbook()
        cases = [
            ({'path': '/Chat/tracker.xlsx', 'sheet': 'Nope', 'append_rows': [['x']]}, 'No sheet'),
            ({'path': '/Chat/tracker.xlsx', 'set_cells': [{'cell': 'nonsense', 'value': 1}]}, 'not a cell'),
            ({'path': '/Chat/tracker.xlsx'}, 'append_rows, set_cells'),
            ({'path': '/Chat/notes.md', 'append_rows': [['x']]}, '.xlsx'),
            ({'path': '/Chat/tracker.xlsx',
              'append_rows': [['=WEBSERVICE("http://x")']]}, 'outside the workbook'),
        ]
        for args, message in cases:
            with self.subTest(message=message):
                self.assertIn(message, self.call('edit_workbook', args)['error'])

    def test_a_missing_file_is_an_error_not_a_new_one(self):
        self.assertIn('No such file', self.call(
            'edit_workbook', {'path': '/Chat/nope.xlsx', 'append_rows': [['x']]})['error'])

    def open(self, path):
        import openpyxl

        return openpyxl.load_workbook(io.BytesIO(vfs.read_binary(self.scope, path)))


# ─────────────────────────────────────────────────────── render_diagram

class DiagramTests(ScopedToolTestCase):
    FLOW = {'title': 'Ingest', 'nodes': [
        {'id': 'a', 'label': 'Upload'}, {'id': 'b', 'label': 'Extract', 'shape': 'diamond'},
        {'id': 'c', 'label': 'Index', 'accent': True}],
        'edges': [{'from': 'a', 'to': 'b', 'label': 'file'}, {'from': 'b', 'to': 'c'}]}

    def test_it_saves_an_svg_with_every_label(self):
        out = self.call('render_diagram', {'path': 'flow.svg', **self.FLOW})
        self.assertEqual((out['nodes'], out['edges']), (3, 2))
        svg = vfs.read_binary(self.scope, '/Chat/flow.svg').decode()
        self.assertTrue(svg.startswith('<svg'))
        for label in ('Upload', 'Extract', 'Index', 'file', 'Ingest'):
            self.assertIn(label, svg)

    def test_layout_is_ours_left_to_right(self):
        spec = diagram.validate(self.FLOW)
        at, _w, _h = diagram._place(spec)
        self.assertLess(at['a'][0], at['b'][0])
        self.assertLess(at['b'][0], at['c'][0])

    def test_a_cycle_is_drawn_rather_than_looping_for_ever(self):
        spec = diagram.validate({'nodes': ['a', 'b'], 'edges': [
            {'from': 'a', 'to': 'b'}, {'from': 'b', 'to': 'a'}]})
        self.assertTrue(diagram.render(spec).startswith(b'<svg'))

    def test_labels_are_escaped(self):
        spec = diagram.validate({'nodes': [{'id': 'a', 'label': '<script>x</script>'}]})
        svg = diagram.render(spec).decode()
        self.assertIn('&lt;script&gt;', svg)
        self.assertNotIn('<script>', svg)

    def test_refusals(self):
        for args, message in (
            ({'nodes': ['a'], 'edges': [{'from': 'a', 'to': 'ghost'}]}, 'not one of the nodes'),
            ({'nodes': ['a'], 'edges': [{'from': 'a', 'to': 'a'}]}, 'self-loop'),
            ({'nodes': [{'id': 'a'}, {'id': 'a'}]}, 'share the id'),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(SpecError, message):
                    diagram.validate(args)


# ─────────────────────────────────────────────────────── download_file

class DownloadTests(ScopedToolTestCase):
    def fake(self, data=b'\x89PNG\r\n\x1a\nrest', mime='image/png'):
        return mock.patch('chat.tools.fetch._fetch', return_value=(data, mime))

    def test_a_download_becomes_a_file_the_other_tools_accept(self):
        with self.fake(), mock.patch('core.safety.net.validate_url_async', return_value=(True, '')):
            out = self.call('download_file', {'url': 'https://example.gov.in/logo.png'})
        self.assertEqual(out['path'], '/Chat/logo.png')
        self.assertTrue(vfs.read_image(self.scope, out['path'])[0].startswith(b'\x89PNG'))
        self.assertIn('Say where it came from', out['rendered'])

    def test_text_keeps_its_text(self):
        with self.fake(b'a,b\n1,2\n', 'text/csv'), \
             mock.patch('core.safety.net.validate_url_async', return_value=(True, '')):
            out = self.call('download_file', {'url': 'https://example.org/data.csv'})
        self.assertEqual(vfs.read_file(self.scope, out['path'])['content'], 'a,b\n1,2\n')

    def test_a_second_download_does_not_overwrite_the_first(self):
        with self.fake(), mock.patch('core.safety.net.validate_url_async', return_value=(True, '')):
            first = self.call('download_file', {'url': 'https://example.gov.in/logo.png'})
            second = self.call('download_file', {'url': 'https://example.gov.in/logo.png'})
        self.assertEqual(first['path'], '/Chat/logo.png')
        self.assertEqual(second['path'], '/Chat/logo (2).png')

    def test_an_internal_address_is_refused_before_any_request(self):
        with mock.patch('chat.tools.fetch._fetch') as fetch:
            out = self.call('download_file', {'url': 'http://169.254.169.254/latest/meta-data'})
        self.assertIn('error', out)
        fetch.assert_not_called()

    def test_it_needs_the_web_grant(self):
        from agents.agent.runtime import GRANT_TOOLS

        self.assertIn('download_file', GRANT_TOOLS['scrape'])


# ─────────────────────────────────────────────────────── notify_user

class NotifyTests(ScopedToolTestCase):
    def test_it_reaches_the_owners_feed(self):
        ctx = {'user_id': self.user.id}
        out = json.loads(async_to_sync(execute_tool)(
            'notify_user', {'title': 'Report ready', 'message': 'Saved to /Chat.',
                            'link': '/documents'}, ctx))
        self.assertTrue(out['sent'])
        from notifications.models import Notification

        notification = Notification.objects.get(user=self.user)
        self.assertEqual((notification.title, notification.type), ('Report ready', 'agent_update'))
        self.assertEqual(notification.data, {'action_url': '/documents'})

    def test_an_off_site_link_is_dropped_not_stored(self):
        ctx = {'user_id': self.user.id}
        async_to_sync(execute_tool)('notify_user', {
            'title': 't', 'message': 'm', 'link': 'https://evil.example/steal'}, ctx)
        from notifications.models import Notification

        self.assertEqual(Notification.objects.get(user=self.user).data, {})

    def test_a_run_cannot_turn_the_feed_into_a_log(self):
        from chat.tools.workspace import MAX_NOTIFICATIONS_PER_RUN

        ctx = {'user_id': self.user.id}
        for _ in range(MAX_NOTIFICATIONS_PER_RUN):
            async_to_sync(execute_tool)('notify_user', {'title': 't', 'message': 'm'}, ctx)
        out = json.loads(async_to_sync(execute_tool)(
            'notify_user', {'title': 't', 'message': 'm'}, ctx))
        self.assertIn('limit', out['error'])
        from notifications.models import Notification

        self.assertEqual(Notification.objects.filter(user=self.user).count(),
                         MAX_NOTIFICATIONS_PER_RUN)


# ─────────────────────────────────────────────────────── extract_data

class ExtractTests(ScopedToolTestCase):
    def test_someone_elses_schema_is_not_usable(self):
        out = self.call('extract_data', {'schema_id': 999, 'paths': ['/Chat/x.md']})
        self.assertIn('No extraction schema', out['error'])

    def test_it_runs_the_engine_over_the_named_documents(self):
        vfs.write_file(self.scope, '/Chat/invoice.md', 'Total: 120')
        from inference.models import ExtractionSchema

        schema = ExtractionSchema.objects.create(user=self.user, name='Invoices')
        with mock.patch('inference.extraction.run_extraction',
                        return_value={'processed': 1, 'created': 1, 'needs_review': 0,
                                      'held_decided': 0, 'errors': []}) as run:
            out = self.call('extract_data', {'schema_id': schema.id, 'paths': ['/Chat/invoice.md']})
        self.assertEqual(out['created'], 1)
        self.assertIn('Invoices', out['rendered'])
        self.assertEqual(run.call_args[0][1], schema.id)

    def test_it_asks_before_running(self):
        self.assertTrue(get('extract_data').sensitive)


# ─────────────────────────────────────────────────────── cost consent

class SpendingAsksFirstTests(SimpleTestCase):
    def test_generating_an_image_is_approved_each_time(self):
        tool = get('generate_image')
        self.assertTrue(tool.sensitive)
        self.assertEqual(tool.effect, 'irreversible')

    def test_the_approval_card_says_it_costs_money(self):
        from chat.tools.describe import describe_call

        self.assertIn('costs money', describe_call('generate_image', {'prompt': 'a harbour'})['title'])


# ─────────────────────────────────────────────────────── per-agent tool scope

class ToolScopeTests(SimpleTestCase):
    def box(self, scope=(), **grants):
        from agents.agent.runtime import AgentToolbox

        return AgentToolbox(grants={'office': True, 'fileOps': True, **grants},
                            user_id=1, file_scope=object(), tool_scope=scope)

    def test_empty_means_every_tool_the_grants_unlock(self):
        names = self.box().allowed_names
        self.assertIn('render_deck', names)
        self.assertIn('write_file', names)

    def test_a_scope_narrows_to_exactly_those_tools(self):
        names = self.box(scope=('render_deck', 'read_file')).allowed_names
        self.assertIn('render_deck', names)
        self.assertIn('read_file', names)
        self.assertNotIn('render_workbook', names)
        self.assertNotIn('write_file', names)

    def test_it_cannot_widen_past_the_grants(self):
        names = self.box(scope=('web_search', 'render_deck')).allowed_names
        self.assertNotIn('web_search', names)

    def test_the_infrastructure_tools_are_never_narrowed_away(self):
        # An agent that may not keep its own plan is not narrower, only more
        # forgetful.
        names = self.box(scope=('render_deck',)).allowed_names
        for always in ('update_todos', 'get_current_time', 'render_chart', 'notify_user',
                       'read_tool_output', 'recall_context'):
            self.assertIn(always, names)

    def test_it_is_read_from_the_agents_config(self):
        from agents.agent.runtime import tool_scope_for

        agent = mock.Mock(agent_context={'toolScope': [' render_deck ', 'read_file', '']})
        self.assertEqual(tool_scope_for(agent), ('read_file', 'render_deck'))
        self.assertEqual(tool_scope_for(mock.Mock(agent_context={})), ())


class ToolScopeApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('api', 'api@example.com', 'pw')
        from rest_framework.test import APIClient

        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def create(self, scope):
        return self.client.post('/api/orchestrator/agents/', {
            'name': 'Scoped', 'brief': 'b', 'tools': {'office': True}, 'toolScope': scope,
        }, format='json')

    def test_a_scope_of_real_tools_round_trips(self):
        response = self.create(['render_deck'])
        self.assertEqual(response.status_code, 201, response.content[:300])
        detail = self.client.get(f"/api/orchestrator/agents/{response.json()['id']}/")
        self.assertEqual(detail.json()['toolScope'], ['render_deck'])

    def test_a_name_no_grant_can_unlock_is_refused(self):
        response = self.create(['render_deck', 'teleport'])
        self.assertEqual(response.status_code, 400)
        self.assertIn('teleport', str(response.json()))
