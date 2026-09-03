"""
Which agents an agent may hand work to — the second axis to the `subAgents` grant.

The gap this closes is the same one connectors had, one floor up and unnoticed
for longer. `subAgents` said *whether* an agent may delegate; nothing said *to
whom*. `search_agents` and `run_agent` filtered on `user_id` and nothing else,
so an agent holding the grant could discover and run every agent its owner had
— including ones holding grants it had itself been refused. That makes
delegation a route to a tool the agent was not given, and the only thing
standing in the way was a sentence in the tool's own description telling the
model not to do it.

Both doors, as everywhere else: discovery is narrowed *and* dispatch re-checks,
because a model names ids it saw in earlier turns.
"""
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from agents.agent.runtime import delegation_scope_for
from agents.models import SubAgent


def agent_with(delegates_to):
    return SubAgent(name='parent', agent_context={'delegatesTo': delegates_to})


class ScopeResolutionTests(SimpleTestCase):
    def test_no_selection_is_unrestricted(self):
        self.assertIsNone(delegation_scope_for(agent_with([])))
        self.assertIsNone(delegation_scope_for(SubAgent(name='t', agent_context={})))
        self.assertIsNone(delegation_scope_for(SubAgent(name='t', agent_context=None)))

    def test_a_selection_becomes_a_sorted_tuple(self):
        self.assertEqual(delegation_scope_for(agent_with([9, 4, 4])), (4, 9))

    def test_a_boolean_is_not_an_agent_id(self):
        # `True == 1`, so a stray boolean would otherwise scope an agent to
        # whichever agent happens to have id 1.
        self.assertIsNone(delegation_scope_for(agent_with([True])))


class DiscoveryAndDispatchTests(TestCase):
    """The tools, with the scope arriving on the tool context."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.allowed = SubAgent.objects.create(
            user=self.user, name='Researcher', description='Reads the web.',
            status='active',
        )
        self.forbidden = SubAgent.objects.create(
            user=self.user, name='Mailer', description='Sends email.',
            status='active', tool_grants={'mcp': True},
        )

    def _search(self, scope):
        from chat.tools.agents import search_agents

        return async_to_sync(search_agents)(
            {}, {'user_id': self.user.id, 'delegation_scope': scope},
        )

    def _run(self, agent_id, scope):
        from chat.tools.agents import run_agent

        return async_to_sync(run_agent)(
            {'agent_id': agent_id, 'goal': 'do it'},
            {'user_id': self.user.id, 'delegation_scope': scope},
        )

    def test_discovery_lists_everything_when_unscoped(self):
        listed = self._search(None)
        self.assertIn('Researcher', listed)
        self.assertIn('Mailer', listed)

    def test_discovery_shows_only_what_is_in_scope(self):
        """Narrowed at discovery as well as at dispatch: an agent that can see a
        name it may not run will keep trying it until the iterations run out."""
        listed = self._search((self.allowed.id,))
        self.assertIn('Researcher', listed)
        self.assertNotIn('Mailer', listed)

    def test_dispatch_refuses_an_agent_outside_the_scope(self):
        """The door that matters. The model names ids from earlier turns, and
        "we didn't list it" has never been access control."""
        result = self._run(self.forbidden.id, (self.allowed.id,))
        self.assertIn('not one this agent may delegate to', result)

    def test_dispatch_says_which_boundary_was_hit(self):
        """Distinct from "no such agent": one is a permission and the other is a
        typo, and a model that cannot tell them apart retries the wrong fix."""
        missing = self._run(999_999, None)
        self.assertIn('belongs to this user', missing)
        refused = self._run(self.forbidden.id, (self.allowed.id,))
        self.assertNotIn('belongs to this user', refused)


class DelegationScopeApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='api', password='pw')
        self.client.force_authenticate(user=self.user)
        self.other_user = User.objects.create_user(username='stranger', password='pw')
        self.mine = SubAgent.objects.create(user=self.user, name='Mine')
        self.theirs = SubAgent.objects.create(user=self.other_user, name='Theirs')
        self.url = reverse('orchestrator:agent_list')

    def _post(self, **kw):
        body = {'name': 'Parent', 'tools': {'subAgents': True}, **kw}
        return self.client.post(self.url, body, format='json')

    def test_a_selection_round_trips(self):
        response = self._post(delegatesTo=[self.mine.id])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['delegatesTo'], [self.mine.id])

    def test_someone_elses_agent_cannot_be_named(self):
        """The same cross-tenant check knowledge bases get: the request is
        authenticated, so nothing downstream would look twice."""
        response = self._post(delegatesTo=[self.theirs.id])
        self.assertEqual(response.status_code, 400)
        self.assertIn('delegatesTo', response.data)

    def test_an_agent_does_not_delegate_to_itself(self):
        created = self._post()
        url = reverse('orchestrator:agent_detail', args=[created.data['id']])
        patched = self.client.patch(
            url, {'delegatesTo': [created.data['id'], self.mine.id]}, format='json',
        )
        self.assertEqual(patched.data['delegatesTo'], [self.mine.id])
