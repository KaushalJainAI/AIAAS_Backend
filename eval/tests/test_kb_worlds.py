"""
E-2: the world's hidden knowledge base.

A world version owns one raw, unindexed `KnowledgeBase` row the listings
never show. The agent reads it through the real KB tools under a scope that
holds exactly its id; the `cited(doc)` grader checks it opened the file its
answer rests on. No provider, no embedder, no indexing job anywhere.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from agents.models import SubAgent
from eval import environment as envmod
from eval import kb_world
from eval import runner
from eval.generator import clean_env_case, prove_case, surfaces_for_agent
from eval.models import EvalCase, EvalSuite, EvalWorld
from inference.models import Document, KnowledgeBase


def _user(name='kbowner'):
    return User.objects.create_user(name, f'{name}@example.com', 'pw')


def _agent(user, name='Writer'):
    return SubAgent.objects.create(
        user=user, name=name,
        tool_grants={'fileOps': True, 'rag': True},
        sandbox={'fileAccess': 'read_all_write_own'},
        guardrails={'autonomy': 'ask'},
        agent_context={'knowledgeBases': []},
        prompt='You answer from policy.')


def _suite(user, agent):
    return EvalSuite.objects.create(
        user=user, name='Policies', subagent=agent, supervision='failures')


KB_FIXTURES = {'documents': [
    {'name': 'refund-policy.md',
     'text': 'Refunds within 30 days need no approval. Beyond 30 days needs Priya.'},
    {'name': 'shipping.md', 'text': 'Ships in 5 days.'},
]}


def _world(suite, version=1, status='accepted', **kwargs):
    defaults = dict(
        brief='Acme Tools policies.',
        surfaces={'files': True, 'kb': True},
        fixtures={'files': {'notes.md': 'See the policies.\n'},
                  'kb': dict(KB_FIXTURES)},
        facts=[{'key': 'refund', 'value': '30 days',
                'statement': 'Refunds within 30 days need no approval.'}],
    )
    defaults.update(kwargs)
    return EvalWorld.objects.create(suite=suite, version=version, status=status,
                                    **defaults)


class EnsureKbTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)
        self.world = _world(self.suite)

    def test_creates_a_raw_hidden_kb(self):
        info = kb_world.ensure_world_kb(
            self.user, self.suite, self.world, self.world.fixtures['kb'])
        kb = KnowledgeBase.objects.get(id=info['id'])
        self.assertEqual(kb.backend, 'raw')
        self.assertTrue(kb_world.is_hidden_kb(kb.name))
        self.assertEqual(info['doc_count'], 2)
        self.assertEqual(
            Document.objects.get(knowledge_base=kb, name='refund-policy.md').status,
            'stored')

    def test_sync_is_idempotent_and_prunes(self):
        kb_world.ensure_world_kb(
            self.user, self.suite, self.world, self.world.fixtures['kb'])
        before = KnowledgeBase.objects.get(
            name=kb_world.kb_name(self.suite.id, 1)).id
        slim = {'documents': [KB_FIXTURES['documents'][0]]}
        info = kb_world.ensure_world_kb(self.user, self.suite, self.world, slim)
        self.assertEqual(info['id'], before)
        self.assertEqual(info['doc_count'], 1)
        self.assertFalse(Document.objects.filter(
            knowledge_base_id=before, name='shipping.md').exists())

    def test_no_fixtures_means_no_kb(self):
        self.assertIsNone(kb_world.ensure_world_kb(
            self.user, self.suite, self.world, {}))

    def test_drop_helpers(self):
        kb_world.ensure_world_kb(
            self.user, self.suite, self.world, self.world.fixtures['kb'])
        kb_world.drop_world_kb(self.user, self.suite.id, 1)
        self.assertFalse(KnowledgeBase.objects.filter(
            name__startswith='.eval/').exists())
        self.assertFalse(Document.objects.filter(
            name='refund-policy.md').exists())


class HiddenKbListingTests(TestCase):
    def setUp(self):
        self.user = _user('lister')
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)
        self.world = _world(self.suite)
        kb_world.ensure_world_kb(
            self.user, self.suite, self.world, self.world.fixtures['kb'])
        KnowledgeBase.objects.create(user=self.user, name='Mine')

    def test_list_knowledge_bases_hides_world_corpora(self):
        from chat.tools.knowledge import list_knowledge_bases
        out = json.loads(async_to_sync(list_knowledge_bases)(
            {}, {'user_id': self.user.id}))
        names = [kb['name'] for kb in out['knowledge_bases']]
        self.assertIn('Mine', names)
        self.assertFalse(any(kb_world.is_hidden_kb(n) for n in names))


class EnvKbTests(TestCase):
    def setUp(self):
        self.user = _user('envkb')
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)
        self.world = _world(self.suite)

    def _env(self):
        env = envmod.for_attempt(self.user, self.agent, self.suite, None)
        env.prepare(None, None, 'r1')
        return env

    def test_scope_and_prompt_point_at_the_hidden_kb(self):
        env = self._env()
        self.assertIsNotNone(env.kb)
        self.assertEqual(env.kb_scope(), (env.kb['id'],))
        self.assertEqual(set(env.kb_docs), {'refund-policy.md', 'shipping.md'})
        gathered = {'knowledge_bases': [{'id': 999, 'name': 'Real'}]}
        swapped = env.filter_gathered(gathered)
        self.assertEqual([kb['id'] for kb in swapped['knowledge_bases']],
                         [env.kb['id']])

    def test_readers_kept_writers_withheld(self):
        from agents.agent.runtime import AgentToolbox
        env = self._env()
        box = AgentToolbox.for_agent(
            self.agent, self.user.id, file_scope=env.attempt_scope(),
            session_key='t', environment=env)
        allowed = set(box.allowed_names)
        for name in ('list_knowledge_bases', 'knowledge_base_search',
                     'keyword_search', 'list_documents', 'read_document'):
            self.assertIn(name, allowed, name)
        for name in ('extract_data', 'ocr_document'):
            self.assertNotIn(name, allowed, name)

    def test_dispatch_reads_the_hidden_doc_through_both_doors(self):
        from agents.agent.runtime import AgentToolbox
        env = self._env()
        box = AgentToolbox.for_agent(
            self.agent, self.user.id, file_scope=env.attempt_scope(),
            session_key='t', environment=env)
        context = {'user_id': self.user.id, 'kb_scope': env.kb_scope(),
                   'file_scope': env.attempt_scope()}
        listed = json.loads(async_to_sync(box.dispatch)(
            'list_documents', {'kb_id': env.kb['id']}, context))
        self.assertEqual(listed['count'], 2)
        doc_id = next(
            d['id'] for d in listed['documents'] if d['name'] == 'refund-policy.md')
        read = async_to_sync(box.dispatch)(
            'read_document', {'document_id': doc_id}, context)
        self.assertIn('30 days', read)

    def test_owner_kbs_unreachable_from_the_world(self):
        mine = KnowledgeBase.objects.create(user=self.user, name='Mine')
        env = self._env()
        context = {'user_id': self.user.id, 'kb_scope': env.kb_scope(),
                   'file_scope': env.attempt_scope()}
        from agents.agent.runtime import AgentToolbox
        box = AgentToolbox.for_agent(
            self.agent, self.user.id, file_scope=env.attempt_scope(),
            session_key='t', environment=env)
        refused = async_to_sync(box.dispatch)(
            'list_documents', {'kb_id': mine.id}, context)
        self.assertIn('not one this agent may search', refused)

    def test_snapshot_carries_kb_docs_for_cited(self):
        env = self._env()
        snap = env.snapshot_env()
        self.assertEqual(set(snap['kb_docs']),
                         {'refund-policy.md', 'shipping.md'})


class CitedGraderTests(SimpleTestCase):
    CTX = {'kb_docs': {'refund-policy.md': 7, 'shipping.md': 9}}

    def _grade(self, doc, tool_trace=(), kb_docs=None):
        from eval import graders
        env = {'kb_docs': dict(self.CTX['kb_docs']) if kb_docs is None else kb_docs}
        return async_to_sync(graders.grade_all)(
            [{'type': 'cited', 'doc': doc}],
            graders.GradeContext(env=env, tool_trace=list(tool_trace)))[0][0]

    def test_read_passes_unread_fails(self):
        trace = [{'tool': 'read_document', 'args': {'document_id': 7}}]
        self.assertTrue(
            self._grade('refund-policy', tool_trace=trace).passed)
        self.assertFalse(self._grade('refund-policy').passed)
        self.assertFalse(self._grade(
            'refund-policy',
            tool_trace=[{'tool': 'read_document',
                         'args': {'document_id': 9}}]).passed)

    def test_unknown_doc_and_missing_map_fail(self):
        grade = self._grade('nope.md')
        self.assertFalse(grade.passed)
        self.assertIn('no world artifact matches', grade.detail)
        grade = self._grade(
            'refund-policy', kb_docs={},
            tool_trace=[{'tool': 'read_document',
                         'args': {'document_id': 7}}])
        self.assertFalse(grade.passed)

    def test_listing_is_not_reading(self):
        trace = [{'tool': 'list_documents', 'args': {}}]
        self.assertFalse(self._grade('refund-policy', tool_trace=trace).passed)


class KbGeneratorTests(SimpleTestCase):
    FACTS = [{'key': 'refund', 'value': '30 days', 'statement': 'Refunds in 30 days.'}]
    TOOLS = {'list_documents', 'read_document', 'list_files'}
    DOCS = ('refund-policy.md', 'shipping.md')

    def test_rag_agent_gets_a_kb_surface(self):
        from types import SimpleNamespace
        agent = SimpleNamespace(
            tool_grants={'rag': True, 'fileOps': True},
            sandbox={'fileAccess': 'scoped'})
        surfaces = surfaces_for_agent(agent)
        self.assertTrue(surfaces['kb'])
        self.assertTrue(surfaces['files'])

    def test_cited_case_cleans_and_proves(self):
        from eval.generator import clean_env_case, prove_case
        raw = dict(
            name='Refund window?', category='normal',
            goal='How long for a no-approval refund?',
            input_data={}, facts_used=['refund'], expected='30 days',
            expect_state={},
            graders=[{'type': 'cited', 'doc': 'refund-policy.md'},
                     {'type': 'contains', 'value': '30 days'}])
        case, why = clean_env_case(raw, self.TOOLS, self.FACTS, self.DOCS)
        self.assertEqual(why, '', why)
        self.assertIn('30 days', case['reference'])
        ok, reason = async_to_sync(prove_case)({'files': {}}, case, self.DOCS)
        self.assertTrue(ok, reason)

    def test_cited_unknown_document_rejected(self):
        from eval.generator import clean_env_case
        raw = dict(
            name='x', category='normal', goal='g', input_data={},
            facts_used=['refund'], expected='30 days', expect_state={},
            graders=[{'type': 'cited', 'doc': 'hallucinated.md'}])
        case, why = clean_env_case(raw, self.TOOLS, self.FACTS, self.DOCS)
        self.assertIsNone(case)
        self.assertIn('not a world document', why)

    def test_impossible_needs_no_content_check(self):
        from eval.generator import clean_env_case
        raw = dict(
            name='Predict?', category='impossible',
            goal='Predict next quarter revenue exactly.',
            input_data={}, facts_used=[], expected='I cannot know that.',
            expect_state={},
            graders=[{'type': 'gave_up', 'expect': True}])
        case, why = clean_env_case(raw, self.TOOLS, self.FACTS, self.DOCS)
        self.assertIsNotNone(case, why)


class KbLifecycleAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.client.force_authenticate(self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='A')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='S', subagent=self.agent)
        self.world = _world(self.suite, status='draft')

    def test_accepting_a_new_version_drops_the_old_kb(self):
        kb_world.ensure_world_kb(
            self.user, self.suite, self.world, self.world.fixtures['kb'])
        self.client.post(reverse('eval:world_accept', args=[self.world.id]))
        v2 = EvalWorld.objects.create(
            suite=self.suite, version=2, status='draft',
            brief='b', surfaces={'files': True, 'kb': True},
            fixtures={'files': {'n.md': 'x'},
                      'kb': {'documents': [{'name': 'v2.md', 'text': 'new'}]}},
            facts=[])
        kb_world.ensure_world_kb(
            self.user, self.suite, v2, v2.fixtures['kb'])
        self.client.post(reverse('eval:world_accept', args=[v2.id])
)
        self.assertFalse(KnowledgeBase.objects.filter(
            name=kb_world.kb_name(self.suite.id, 1)).exists())
        self.assertTrue(KnowledgeBase.objects.filter(
            name=kb_world.kb_name(self.suite.id, 2)).exists())

    def test_suite_delete_drops_hidden_kbs(self):
        kb_world.ensure_world_kb(
            self.user, self.suite, self.world, self.world.fixtures['kb'])
        self.client.delete(reverse('eval:suite_detail', args=[self.suite.id]))
        self.assertFalse(KnowledgeBase.objects.filter(
            name__startswith='.eval/').exists())


class KbSweepTests(TestCase):
    def test_writer_case_grades_which_policy_applies(self):
        from agents.agent.runtime import AgentRun
        user = _user('sweepkb')
        agent = _agent(user)
        suite = _suite(user, agent)
        world = _world(suite)
        case = EvalCase.objects.create(
            suite=suite, goal='How long for a no-approval refund?',
            is_active=True, world_version=1,
            reference='Correct answer: 30 days',
            graders=[{'type': 'cited', 'doc': 'refund-policy.md'},
                     {'type': 'contains', 'value': '30 days'}])

        async def fake(agent, goal, **kwargs):
            env = kwargs['environment']
            doc_id = env.kb_docs['refund-policy.md']
            return AgentRun(
                execution_id='00000000-0000-0000-0000-000000000001',
                answer='30 days, per the refund policy.', thinking='',
                tool_trace=[{'tool': 'list_documents', 'args': {},
                             'iteration': 1},
                            {'tool': 'read_document',
                             'args': {'document_id': doc_id},
                             'iteration': 2}],
                tokens=10, awaiting_approval=False, unserved_grants=(),
                duration_ms=50)

        from unittest.mock import patch
        run = async_to_sync(runner.open_run)(suite, agent, user, '')
        with patch('agents.agent.runtime.run_agent', fake):
            async_to_sync(runner.sweep)(run, suite, agent, user)
        run.refresh_from_db()
        result = run.results.get()
        self.assertTrue(result.auto_passed, result.grades)
