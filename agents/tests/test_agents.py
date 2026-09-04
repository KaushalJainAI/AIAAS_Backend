"""
Agents API tests.

The cases worth having are the ones where a mistake is invisible in the UI: a
grant that silently widens, a knowledge base that belongs to someone else, an
agent that shows up in the canvas list. Round-tripping a config is the
cheap half; the ownership and merge cases are the half that matters.
"""
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from inference.models import KnowledgeBase
from mcp_integration.models import MCPServer
from agents.models import SubAgent


def curated(name):
    """The id of a curated connection, which migrations guarantee exists.

    Connectors are picked by `MCPServer` id now, not by a slug from a list in
    code, so a test that names one has to name a row. `mcp_integration.0005`
    seeds these and `test_fresh_install.py` is what fails if it stops doing so.
    """
    return MCPServer.objects.get(name=name, user__isnull=True).id


def config(**overrides):
    """A minimal valid agent, plus whatever the test is actually about."""
    base = {
        'name': 'Finance agent',
        'brief': 'Reads invoices and chases what is overdue.',
        'provider': 'openrouter',
        'model': 'anthropic/claude-sonnet-5',
        'temperature': 0,
        'tools': {'codeExecution': True, 'rag': True},
        'autonomy': 'ask',
    }
    base.update(overrides)
    return base


class AgentCrudTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.list_url = reverse('orchestrator:agent_list')

    def test_create_returns_the_config_it_was_given(self):
        r = self.client.post(self.list_url, config(), format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data['name'], 'Finance agent')
        self.assertEqual(r.data['tools']['codeExecution'], True)
        self.assertEqual(r.data['autonomy'], 'ask')
        # Stats exist from the first read, at zero, so the card never has to
        # guard against a missing field.
        self.assertEqual(r.data['runs'], 0)

    def test_unsent_tools_are_stored_as_denied_not_absent(self):
        """A grant the caller never mentioned must read as 'no', not 'unset'."""
        r = self.client.post(self.list_url, config(tools={'rag': True}), format='json')
        agent = SubAgent.objects.get(id=r.data['id'])
        self.assertEqual(agent.tool_grants['shell'], False)
        self.assertEqual(agent.tool_grants['codeExecution'], False)
        self.assertEqual(sorted(agent.tool_grants), sorted(agent.tool_grants))
        self.assertIn('webSearch', agent.tool_grants)

    def test_round_trip_preserves_every_knob(self):
        sent = config(
            fileAccess='readonly', maxRunSeconds=1800,
            description='Chases overdue invoices and reports what is stuck.',
            tags=['finance', 'weekly'],
            connectors=sorted([curated('Gmail'), curated('Google Sheets')]),
            useEnvironment=True,
            outputContract='extraction', fanoutParallel=3, status='paused',
            schedule='0 9 * * 1', allowUnattended=True,
            notifyOnHitl=False, reviewAgent=True, spendCapRupees=750,
            recursiveContext=False, compaction=False, indexing=False,
        )
        created = self.client.post(self.list_url, sent, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        fetched = self.client.get(
            reverse('orchestrator:agent_detail', args=[created.data['id']])
        )
        for key, value in sent.items():
            if key == 'tools':
                continue
            self.assertEqual(fetched.data[key], value, f'{key} did not survive the round trip')

    def test_patch_merges_rather_than_resetting_unsent_knobs(self):
        created = self.client.post(
            self.list_url,
            config(spendCapRupees=900, connectors=[curated('Gmail')]),
            format='json',
        )
        url = reverse('orchestrator:agent_detail', args=[created.data['id']])

        r = self.client.patch(url, {'temperature': 0.7}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data['temperature'], 0.7)
        # The knobs the caller did not send are still what they were.
        self.assertEqual(r.data['spendCapRupees'], 900)
        self.assertEqual(r.data['connectors'], [curated('Gmail')])
        self.assertEqual(r.data['tools']['codeExecution'], True)

    def test_delete(self):
        created = self.client.post(self.list_url, config(), format='json')
        url = reverse('orchestrator:agent_detail', args=[created.data['id']])
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_duplicate_name_is_suffixed_not_rejected(self):
        self.client.post(self.list_url, config(), format='json')
        second = self.client.post(self.list_url, config(), format='json')
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.data['name'], 'Finance agent (1)')


class AgentValidationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.list_url = reverse('orchestrator:agent_list')

    def _post(self, **kw):
        return self.client.post(self.list_url, config(**kw), format='json')

    def test_the_invocation_mode_is_derived_from_the_schedule(self):
        """`trigger` came off the wire (2026-09-03).

        Nothing in the runtime ever branched on it, and the serializer required
        a schedule for `maintenance` — so the schedule was already the answer
        and the field was a second place to contradict it. It is still emitted,
        derived, because the builder shows how an agent is invoked.
        """
        plain = self._post()
        self.assertEqual(plain.data['trigger'], 'goal')

        scheduled = self._post(schedule='0 9 * * 1', allowUnattended=True)
        self.assertEqual(scheduled.data['trigger'], 'maintenance')

        # And a client still sending the old field cannot set it.
        ignored = self._post(trigger='maintenance')
        self.assertEqual(ignored.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ignored.data['trigger'], 'goal')

    def test_malformed_cron_is_refused(self):
        r = self._post(schedule='every monday')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_schedule_without_unattended_clearance_is_refused(self):
        """The failure this replaces was silent: the Trigger row was created and
        armed, the sweep fired it, the runtime refused it, and after five
        refusals the trigger disabled itself. Nothing surfaced."""
        r = self._post(schedule='0 9 * * 1')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('allowUnattended', r.data)
        self.assertFalse(SubAgent.objects.exists())

    def test_a_cleared_schedule_is_accepted_and_arms_a_trigger(self):
        r = self._post(schedule='0 9 * * 1', allowUnattended=True)
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        agent = SubAgent.objects.get(id=r.data['id'])
        self.assertTrue(agent.allow_unattended)
        trigger = agent.triggers.get(mode='schedule')
        self.assertEqual(trigger.cron, '0 9 * * 1')
        self.assertIsNotNone(trigger.next_due_at)

    def test_unattended_defaults_off_when_unmentioned(self):
        """A request that does not name the gate is not a request to open it."""
        r = self._post()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertFalse(SubAgent.objects.get(id=r.data['id']).allow_unattended)
        self.assertFalse(r.data['allowUnattended'])

    def test_unattended_survives_a_patch_that_does_not_mention_it(self):
        """PATCH merges `to_config`, so the gate must not silently close."""
        created = self._post(schedule='0 9 * * 1',
                             allowUnattended=True)
        url = reverse('orchestrator:agent_detail', args=[created.data['id']])
        r = self.client.patch(url, {'temperature': 0.5}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(r.data['allowUnattended'])
        self.assertTrue(SubAgent.objects.get(id=created.data['id']).allow_unattended)

    def test_unknown_tool_key_is_refused(self):
        r = self._post(tools={'rootkit': True})
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_connector_is_refused(self):
        r = self._post(connectors=[9_999_999])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_users_connection_is_refused(self):
        """A connection id is not a capability just because it is an integer.

        The whole point of the selection is to narrow what an agent reaches, so
        naming someone else's server has to be refused here — the runtime
        intersects with the user's visible servers too, but a builder that
        accepts a choice the toolbox will silently drop is its own bug.
        """
        stranger = User.objects.create_user(username='stranger', password='pw')
        theirs = MCPServer.objects.create(
            user=stranger, name='Their box', type='stdio', command='npx',
        )
        r = self._post(connectors=[theirs.id])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_sandbox_holds_only_file_access(self):
        """`workdir` and `venv` came off the wire with `cpu`/`memoryMb` before
        them, for the same reason: stored, validated, and read by nothing. A
        client still sending them must not be able to put them back."""
        r = self._post(workdir='/workspace/../../etc', venv=False)
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        agent = SubAgent.objects.get(id=r.data['id'])
        self.assertEqual(agent.sandbox, {'fileAccess': 'scoped'})
        self.assertNotIn('workdir', r.data)
        self.assertNotIn('venv', r.data)

    def test_egress_is_no_longer_settable(self):
        """It was read in one place — to add a sentence to the system prompt —
        and `allowlist`/`full` could never have been honoured: the sandbox is a
        sidecar on an internal-only Docker network. The prompt line is now
        unconditional, which is the true statement."""
        r = self._post(tools={'shell': True}, egress='full')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        agent = SubAgent.objects.get(id=r.data['id'])
        self.assertNotIn('egress', agent.guardrails)
        self.assertNotIn('egress', r.data)

    def test_a_result_contract_outside_the_registry_is_refused(self):
        r = self._post(outputContract='freeform-json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_tags_are_trimmed_and_deduplicated(self):
        r = self._post(tags=['  Finance ', 'finance', 'Weekly'])
        self.assertEqual(r.data['tags'], ['Finance', 'Weekly'])

    def test_a_paused_agent_can_be_saved_and_resumed(self):
        """`status` was read-only, so the only way to stop a scheduled agent
        was to delete its schedule or the agent."""
        created = self._post(status='paused')
        self.assertEqual(created.data['status'], 'paused')
        url = reverse('orchestrator:agent_detail', args=[created.data['id']])
        resumed = self.client.patch(url, {'status': 'active'}, format='json')
        self.assertEqual(resumed.data['status'], 'active')

    def test_archiving_is_not_offered_as_a_save(self):
        """There is no un-archive path, so a dropdown that reaches it is a
        one-way door in disguise."""
        r = self._post(status='archived')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_run_limit_outside_the_range_is_refused(self):
        # The ceiling that replaced `memoryMb`'s. Both directions: a run limit
        # of two seconds is not a stricter agent, it is one that can never
        # finish, and a limit of a week is not a limit.
        self.assertEqual(self._post(maxRunSeconds=1).status_code,
                         status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._post(maxRunSeconds=999_999).status_code,
                         status.HTTP_400_BAD_REQUEST)

    def test_blank_name_is_refused(self):
        self.assertEqual(self._post(name='   ').status_code, status.HTTP_400_BAD_REQUEST)


class AgentIsolationTests(APITestCase):
    """Cross-tenant cases. Each of these would be a silent data leak."""

    def setUp(self):
        self.owner = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='other', password='pw')
        self.list_url = reverse('orchestrator:agent_list')

    def test_cannot_attach_another_users_knowledge_base(self):
        their_kb = KnowledgeBase.objects.create(user=self.other, name='Their vendors')
        self.client.force_authenticate(user=self.owner)

        r = self.client.post(self.list_url, config(knowledgeBases=[their_kb.id]), format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('knowledgeBases', r.data)

    def test_can_attach_own_knowledge_base(self):
        mine = KnowledgeBase.objects.create(user=self.owner, name='My vendors')
        self.client.force_authenticate(user=self.owner)

        r = self.client.post(self.list_url, config(knowledgeBases=[mine.id]), format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data['knowledgeBases'], [mine.id])

    def test_another_users_agent_is_not_listed_or_readable(self):
        self.client.force_authenticate(user=self.owner)
        created = self.client.post(self.list_url, config(), format='json')

        self.client.force_authenticate(user=self.other)
        self.assertEqual(self.client.get(self.list_url).data, [])
        detail = reverse('orchestrator:agent_detail', args=[created.data['id']])
        self.assertEqual(self.client.get(detail).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(detail).status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_access_is_refused(self):
        self.assertIn(
            self.client.get(self.list_url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


