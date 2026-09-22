"""
C1 — the two new per-agent limits: `writePaths` and `commandScope`.

Both follow the existing grant/scope pattern: empty means unrestricted (agents
predate them), and a worker never holds more than its template allows or more
than its parent holds (most-restrictive-wins in `invoke_subagent`).
"""
from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from agents.agent.runtime import (
    command_scope_for,
    intersect_command_scope,
    intersect_write_paths,
    playbooks_for,
    write_paths_for,
)
from agents.models import SubAgent
from agents.playbooks import PLAYBOOK_SLUGS, load


class WritePathsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)

    def _agent(self, name='A', **ctx):
        return SubAgent.objects.create(
            user=self.user, name=name, agent_context=ctx)

    def test_empty_means_unrestricted(self):
        agent = self._agent()
        self.assertIsNone(write_paths_for(agent))
        self.assertIsNone(command_scope_for(agent))

    def test_intersection_never_widens(self):
        # A worker on `src/api/**` under a lead on `src/**` keeps the narrower.
        self.assertEqual(
            intersect_write_paths(('src/**',), ('src/api/**',)), ('src/api/**',))
        self.assertEqual(
            intersect_write_paths(('src/api/**',), ('src/**',)), ('src/api/**',))
        # Disjoint restrictions intersect to "may write nothing" — `()`, never
        # unrestricted (None), which would widen the refused workers.
        self.assertEqual(
            intersect_write_paths(('src/**',), ('tests/**',)), ())
        self.assertIsNone(intersect_write_paths(None, None))
        self.assertEqual(
            intersect_write_paths(('src/**',), None), ('src/**',))
        self.assertEqual(
            intersect_write_paths(None, ('src/**',)), ('src/**',))
        self.assertEqual(
            intersect_command_scope(('test', 'lint'), ('test',)), ('test',))
        self.assertEqual(
            intersect_command_scope(('test',), None), ('test',))
        self.assertIsNone(intersect_command_scope(None, None))
        # `any` on either side imposes no class limit of its own.
        self.assertEqual(
            intersect_command_scope(('any',), ('test',)), ('test',))

    def test_unknown_command_classes_are_refused_at_save(self):
        response = self.client.post('/api/orchestrator/agents/', {
            'name': 'Cmd', 'commandScope': ['teleport'],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_known_scopes_round_trip_through_the_serializer(self):
        response = self.client.post('/api/orchestrator/agents/', {
            'name': 'Scoped',
            'tools': {'shell': True},
            'writePaths': ['src/api/**'],
            'commandScope': ['test', 'lint'],
            'playbooks': ['small-diffs'],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['writePaths'], ['src/api/**'])
        self.assertEqual(response.data['commandScope'], ['lint', 'test'])
        self.assertEqual(response.data['playbooks'], ['small-diffs'])

    def test_an_unknown_playbook_is_refused_not_dropped(self):
        response = self.client.post('/api/orchestrator/agents/', {
            'name': 'Play', 'playbooks': ['no-such-playbook'],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_a_lead_cannot_grant_wider_limits_than_it_holds(self):
        # The merge itself: the worker's effective set is the intersection.
        parent = ('src/api/**',)
        worker = None
        self.assertEqual(intersect_write_paths(parent, worker), parent)
        worker_wide = ('src/**', 'tests/**')
        self.assertEqual(
            intersect_write_paths(parent, worker_wide), ('src/api/**',))

    def test_contracts_resolve(self):
        from agents import contracts

        self.assertIsNotNone(contracts.get('code_plan'))
        self.assertIsNotNone(contracts.get('patch'))

    def test_code_plan_repair_coerces_a_single_task(self):
        from agents import contracts

        plan = contracts.coerce(
            '{"goal": "g", "tasks": {"id": "t1", "title": "one"}}',
            contracts.get('code_plan'))
        self.assertEqual(len(plan['tasks']), 1)
        self.assertEqual(plan['tasks'][0]['id'], 't1')

    def test_every_playbook_slug_loads(self):
        for slug in PLAYBOOK_SLUGS:
            self.assertTrue(load(slug), f'playbook {slug} is empty')

    def test_playbooks_for_drops_unknown_slugs(self):
        agent = self._agent(name='P', playbooks=['small-diffs', 'nope'])
        self.assertEqual(playbooks_for(agent), ['small-diffs'])
