"""
The builder's chat: what a model may and may not do to a configuration.

The interesting cases are all the same shape — the model is a stranger writing
into a permissions object, so every test here is about what happens when it
writes something wrong. A proposal that reaches the board has to be one the
user can then save, and one that never widens the agent by accident.

The provider is stubbed throughout: these assert on what we do with an answer,
which is the part that is ours.
"""
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from agents.views.builder import (
    KNOBS,
    Catalogue,
    enforce_couplings,
    parse_reply,
    sanitise,
)
from agents.views.agents import TOOL_KEYS
from inference.models import KnowledgeBase
from mcp_integration.models import MCPServer

URL = '/api/orchestrator/agents/configure/'


class Completion:
    """The one field of `llm.Completion` this path reads."""

    def __init__(self, content):
        self.content = content
        self.tokens = 0


def answer(reply='Set.', changes=()):
    return Completion(json.dumps({'reply': reply, 'changes': list(changes)}))


class SanitiseTests(APITestCase):
    """The validator, without the HTTP or the provider."""

    def setUp(self):
        self.cat = Catalogue(
            connectors=[{'id': 7, 'label': 'Gmail'}],
            knowledge_bases=[{'id': 3, 'label': 'Policies'}],
            skills=[],
        )

    def _sanitise(self, changes, cfg=None):
        return sanitise(changes, cfg or {}, self.cat)

    def test_an_unknown_path_is_dropped(self):
        """A knob we do not have is not a knob a model may invent — and it is
        dropped rather than passed through, because the board applies changes by
        path and would write a key the serializer then rejects on save."""
        out = self._sanitise([
            {'path': 'tools.telepathy', 'value': True},
            {'path': 'admin', 'value': True},
            {'path': 'tools.webSearch', 'value': True},
        ])
        self.assertEqual([c['path'] for c in out], ['tools.webSearch'])

    def test_a_value_outside_the_closed_set_is_dropped(self):
        out = self._sanitise([
            {'path': 'autonomy', 'value': 'yolo'},
            {'path': 'fileAccess', 'value': 'everything'},
            {'path': 'temperature', 'value': 9},
            {'path': 'outputContract', 'value': 'freeform-json'},
            {'path': 'fileAccess', 'value': 'readonly'},
        ])
        self.assertEqual([c['path'] for c in out], ['fileAccess'])

    def test_an_id_the_user_cannot_see_is_refused_whole(self):
        """Not narrowed to the ids that do resolve: a partial selection is a
        different selection, and an agent pointed at some of its sources
        answers confidently from the wrong corpus."""
        self.assertEqual(self._sanitise([{'path': 'connectors', 'value': [7, 99]}]), [])
        out = self._sanitise([{'path': 'connectors', 'value': [7]}])
        self.assertEqual(out[0]['value'], [7])

    def test_an_impossible_cron_is_dropped(self):
        self.assertEqual(self._sanitise([{'path': 'schedule', 'value': 'every morning'}]), [])
        out = self._sanitise([{'path': 'schedule', 'value': ' 0 9 * * 1-5 '}])
        self.assertEqual(out[0]['value'], '0 9 * * 1-5')

    def test_a_change_that_changes_nothing_is_not_shown(self):
        """The board highlights what moved. A restated value highlights a knob
        the user never touched and buries the ones that did."""
        cfg = {'autonomy': 'ask', 'tools': {'webSearch': True}}
        out = self._sanitise([
            {'path': 'autonomy', 'value': 'ask'},
            {'path': 'tools.webSearch', 'value': True},
            {'path': 'tools.rag', 'value': True},
        ], cfg)
        self.assertEqual([c['path'] for c in out], ['tools.rag'])

    def test_the_label_is_ours_and_the_reason_is_the_models(self):
        out = self._sanitise([
            {'path': 'autonomy', 'value': 'full', 'label': 'TOTALLY SAFE',
             'why': 'You asked to be left out of it.'},
        ])
        self.assertEqual(out[0]['label'], KNOBS['autonomy'].label)
        self.assertEqual(out[0]['why'], 'You asked to be left out of it.')

    def test_junk_in_place_of_a_change_list_is_survivable(self):
        for junk in (None, 'changes', {'path': 'name'}, [None, 3, 'x']):
            self.assertEqual(sanitise(junk, {}, self.cat), [])

    def test_every_grant_the_runtime_knows_can_be_set_and_is_described(self):
        """A tool the runtime hands out but the prompt never mentions is one the
        configuring model can only reach by guessing its name."""
        described = {p.split('.', 1)[1] for p in KNOBS if p.startswith('tools.')}
        self.assertEqual(described, TOOL_KEYS)


    def test_the_knobs_the_runtime_reads_are_all_settable(self):
        """The four fields exposed in Phase 1 are the ones the runtime has
        always read and the builder could never set — `description` and `tags`
        drive `search_agents`, the contract drives `contracts.resolve`, and the
        fan-out width drives `run_fanout`."""
        for path in ('description', 'tags', 'outputContract', 'fanoutParallel',
                     'status'):
            self.assertIn(path, KNOBS)

    def test_the_retired_knobs_cannot_be_proposed(self):
        """A model that has seen an older config must not be able to put a
        removed control back on the board."""
        out = self._sanitise([
            {'path': 'egress', 'value': 'full'},
            {'path': 'workdir', 'value': '/tmp'},
            {'path': 'venv', 'value': False},
            {'path': 'useOrgContext', 'value': True},
            {'path': 'trigger', 'value': 'maintenance'},
        ])
        self.assertEqual(out, [])


class ParseTests(APITestCase):
    def test_json_is_found_through_fences_and_prose(self):
        for raw in (
            '{"reply": "hi", "changes": []}',
            '```json\n{"reply": "hi", "changes": []}\n```',
            'Sure!\n```\n{"reply": "hi", "changes": []}\n```\nHope that helps.',
            'Here you go: {"reply": "hi", "changes": []}',
        ):
            self.assertEqual(parse_reply(raw).get('reply'), 'hi')

    def test_an_answer_with_no_json_is_none(self):
        """None is what makes the view answer 503 and the browser fall back to
        its local rules — an empty proposal would look like "nothing to do"."""
        for raw in ('', 'I would enable web search.', '[1, 2, 3]'):
            self.assertIsNone(parse_reply(raw))


class CouplingTests(APITestCase):
    """The rules that live on the serializer, enforced through the serializer."""

    def setUp(self):
        self.user = User.objects.create_user('coupled', 'c@example.com', 'pw')
        self.client.force_authenticate(self.user)

    def _request(self):
        from rest_framework.test import APIRequestFactory
        from rest_framework.request import Request

        request = Request(APIRequestFactory().post('/'))
        request.user = self.user
        return request

    def test_a_schedule_without_unattended_is_not_proposed(self):
        """`AgentSerializer` refuses that pair, so proposing it would light up
        the board and then 400 on save with nothing on screen saying which of
        the highlighted knobs was the problem."""
        changes = [
            {'path': 'schedule', 'label': 'Schedule', 'value': '0 9 * * 1', 'why': ''},
        ]
        kept = enforce_couplings(list(changes), {'name': 'A'}, self._request())
        self.assertNotIn('schedule', [c['path'] for c in kept])

        with_gate = changes + [
            {'path': 'allowUnattended', 'label': 'Unattended', 'value': True, 'why': ''},
        ]
        kept = enforce_couplings(with_gate, {'name': 'A'}, self._request())
        self.assertEqual(
            {c['path'] for c in kept},
            {'schedule', 'allowUnattended'},
        )

    def test_a_result_shape_outside_the_registry_cannot_be_proposed(self):
        """Contracts are a closed registry because the UI renders them — a
        shape nothing can display is a promise the product cannot keep."""
        changes = [
            {'path': 'outputContract', 'label': 'Result shape',
             'value': 'freeform-json', 'why': ''},
        ]
        kept = enforce_couplings(changes, {'name': 'A'}, self._request())
        self.assertEqual(kept, [])


class ConfigureEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('builder', 'b@example.com', 'pw')
        self.client.force_authenticate(self.user)
        self.kb = KnowledgeBase.objects.create(user=self.user, name='Policies')

    def test_it_applies_a_proposal_to_the_board(self):
        proposal = answer('Set up for inbox triage.', [
            {'path': 'name', 'value': 'Inbox triage', 'why': 'It reads mail.'},
            {'path': 'tools.rag', 'value': True, 'why': 'It searches your docs.'},
            {'path': 'knowledgeBases', 'value': [self.kb.id], 'why': 'The corpus you named.'},
        ])
        with patch('llm.access.complete', return_value=proposal):
            response = self.client.post(
                URL, {'message': 'triage my inbox against our policies',
                      'config': {'name': '', 'tools': {}}}, format='json')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['source'], 'model')
        self.assertEqual(
            {c['path'] for c in body['changes']},
            {'name', 'tools.rag', 'knowledgeBases'},
        )

    def test_someone_elses_knowledge_base_cannot_be_attached(self):
        """The catalogue is built from the caller, so an id belonging to another
        account is not in it — which is the same check `AgentSerializer` makes
        on save, made here before the board ever shows it."""
        stranger = User.objects.create_user('stranger', 's@example.com', 'pw')
        theirs = KnowledgeBase.objects.create(user=stranger, name='Theirs')
        proposal = answer(changes=[{'path': 'knowledgeBases', 'value': [theirs.id]}])
        with patch('llm.access.complete', return_value=proposal):
            response = self.client.post(URL, {'message': 'use the other corpus'},
                                        format='json')
        self.assertEqual(response.json()['changes'], [])

    def test_a_connection_the_user_does_not_have_is_not_proposed(self):
        MCPServer.objects.filter(user__isnull=True).update(enabled=False)
        proposal = answer(changes=[{'path': 'connectors', 'value': [1234]}])
        with patch('llm.access.complete', return_value=proposal):
            response = self.client.post(URL, {'message': 'read my gmail'}, format='json')
        self.assertEqual(response.json()['changes'], [])

    def test_no_model_available_is_a_503_so_the_client_can_fall_back(self):
        """Not a 200 with an apology: the browser keeps a local rule table for
        exactly this, and it can only use it if the failure is legible."""
        with patch('llm.access.complete', side_effect=RuntimeError('no key')):
            response = self.client.post(URL, {'message': 'do a thing'}, format='json')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'builder_model_unavailable')

    def test_an_unparseable_answer_is_also_a_503(self):
        with patch('llm.access.complete', return_value=Completion('I would turn on RAG.')):
            response = self.client.post(URL, {'message': 'do a thing'}, format='json')
        self.assertEqual(response.status_code, 503)

    def test_a_blank_message_is_refused(self):
        response = self.client.post(URL, {'message': '   '}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_it_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.post(URL, {'message': 'hi'}, format='json').status_code,
                      (401, 403))

    def test_the_prompt_carries_the_board_and_the_catalogue(self):
        """The whole reason this is a server endpoint: ids exist only here, and
        a model that cannot see the current config restates values it already
        has."""
        captured = {}

        async def fake_complete(**kwargs):
            captured.update(kwargs)
            return answer()

        with patch('llm.access.complete', side_effect=fake_complete):
            self.client.post(URL, {
                'message': 'search our policies',
                'config': {'name': 'Existing', 'autonomy': 'ask'},
            }, format='json')

        self.assertIn('Policies', captured['system_message'])
        self.assertIn(str(self.kb.id), captured['system_message'])
        self.assertIn('Existing', captured['prompt'])
        # Deterministic: the same brief should not produce a different board on
        # a second read.
        self.assertEqual(captured['temperature'], 0)
