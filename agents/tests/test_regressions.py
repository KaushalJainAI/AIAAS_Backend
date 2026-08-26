"""
Regressions for bugs that shipped because nothing exercised the path.

Every test here failed before the fix in the same change. They are grouped by
the thing that made each bug possible rather than by module, because that is
what says how to avoid the next one: several are a rename the callers were
never updated for, and two are an ORM aggregate whose join nobody read.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from agents.models import ConversationMessage, HITLRequest, SubAgent, Trigger
from logs.models import ExecutionLog

User = get_user_model()


class RenamedColumnTests(APITestCase):
    """`Workflow` became `SubAgent`; these call sites were left behind.

    Both were 500s on every request, on endpoints the frontend calls, and both
    are the kind of break a rename leaves in a string argument or a keyword
    argument name, where no type checker looks.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)

    def test_pending_hitl_does_not_select_related_a_dead_field(self):
        agent = SubAgent.objects.create(user=self.user, name='A')
        log = ExecutionLog.objects.create(subagent=agent, user=self.user,
                                          status='paused')
        HITLRequest.objects.create(execution=log, user=self.user,
                                   request_type='approval', title='t',
                                   message='m', status='pending')

        response = self.client.get('/api/orchestrator/hitl/pending/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        # The serializer field keeps its wire name; what it names is the agent.
        self.assertEqual(response.data['requests'][0]['workflow_name'], 'A')

    def test_posting_a_chat_message_stores_the_agent(self):
        agent = SubAgent.objects.create(user=self.user, name='A')

        response = self.client.post(
            '/api/orchestrator/chat/',
            {'content': 'hello', 'workflow_id': agent.id}, format='json',
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(ConversationMessage.objects.get().subagent_id, agent.id)

    def test_posting_a_chat_message_without_an_agent_works(self):
        response = self.client.post('/api/orchestrator/chat/',
                                    {'content': 'hello'}, format='json')
        self.assertEqual(response.status_code, 202)

    def test_a_chat_message_cannot_be_attached_to_someone_elses_agent(self):
        other = User.objects.create_user(username='other', password='pw')
        theirs = SubAgent.objects.create(user=other, name='Theirs')

        response = self.client.post(
            '/api/orchestrator/chat/',
            {'content': 'hello', 'workflow_id': theirs.id}, format='json',
        )

        self.assertEqual(response.status_code, 202)
        self.assertIsNone(ConversationMessage.objects.get().subagent_id)

    def test_a_junk_agent_id_does_not_500(self):
        response = self.client.post(
            '/api/orchestrator/chat/',
            {'content': 'hello', 'workflow_id': 'not-a-number'}, format='json',
        )
        self.assertEqual(response.status_code, 202)


class AgentStatsTests(APITestCase):
    """The listing's numbers, over the LEFT JOIN that `hitl_requests` forces.

    A non-distinct Count over that join counts execution-by-HITL pairs, so one
    run with three approvals was reported as three runs, and the same join
    multiplied the spend Sum by three.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='A')

    def _run(self, *, hitl: int = 0, tokens: int = 0):
        log = ExecutionLog.objects.create(subagent=self.agent, user=self.user,
                                          status='completed', tokens_used=tokens)
        for i in range(hitl):
            HITLRequest.objects.create(execution=log, user=self.user,
                                       request_type='approval', title=f't{i}',
                                       message='m', status='approved')
        return log

    def test_hitl_requests_do_not_multiply_the_run_count(self):
        self._run(hitl=3)
        self._run(hitl=0)

        stats = self.client.get('/api/orchestrator/agents/').data[0]

        self.assertEqual(stats['runs'], 2)
        # Exactly one of the two needed no human.
        self.assertEqual(stats['unattended'], 1)

    def test_hitl_requests_do_not_multiply_the_spend(self):
        from agents.spend import rupees_for

        self._run(hitl=3, tokens=2_000_000)

        stats = self.client.get('/api/orchestrator/agents/').data[0]

        self.assertEqual(stats['spend'], rupees_for(2_000_000))

    def test_spend_is_the_number_the_cap_refuses_on(self):
        """The displayed spend and the enforced spend must be one number."""
        from asgiref.sync import async_to_sync

        from agents.agent.runtime import AgentRunRefused, check_guardrails

        self._run(tokens=4_000_000)
        shown = self.client.get('/api/orchestrator/agents/').data[0]['spend']
        self.assertGreater(shown, 0)

        self.agent.guardrails = {'spendCapRupees': shown}
        self.agent.save(update_fields=['guardrails'])
        with self.assertRaises(AgentRunRefused):
            async_to_sync(check_guardrails)(self.agent, self.user)


class RevisionOrderingTests(APITestCase):
    """A revision has to snapshot the configuration as saved, schedule included."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)

    def test_an_edited_schedule_lands_in_that_edits_revision(self):
        create = self.client.post('/api/orchestrator/agents/', {
            'name': 'Sched', 'brief': 'b', 'schedule': '0 9 * * 1',
            'allowUnattended': True,
        }, format='json')
        self.assertEqual(create.status_code, 201)
        agent_id = create.data['id']

        patch = self.client.patch(f'/api/orchestrator/agents/{agent_id}/',
                                  {'schedule': '0 18 * * 5'}, format='json')
        self.assertEqual(patch.status_code, 200)

        from logs.models import SubAgentRevision
        latest = (SubAgentRevision.objects
                  .filter(subagent_id=agent_id).order_by('-number').first())
        # Recorded before sync_schedule, this said "0 9 * * 1" — the schedule
        # the edit had just replaced.
        self.assertEqual(latest.config['schedule'], '0 18 * * 5')


class WebhookFailureTests(APITestCase):
    """The webhook receiver promised to disable itself and never counted."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        # Not cleared for unattended runs, so every firing is refused.
        self.agent = SubAgent.objects.create(user=self.user, name='A',
                                             prompt='do it',
                                             allow_unattended=False)
        self.trigger = Trigger.objects.create(subagent=self.agent, mode='webhook',
                                              goal='go')

    def test_repeated_refusals_disable_the_trigger(self):
        from agents.sweep import MAX_CONSECUTIVE_FAILURES

        url = f'/api/orchestrator/hooks/{self.trigger.secret}/'
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            self.assertEqual(self.client.post(url, {}, format='json').status_code, 404)

        self.trigger.refresh_from_db()
        self.assertEqual(self.trigger.consecutive_failures, MAX_CONSECUTIVE_FAILURES)
        self.assertFalse(self.trigger.enabled)


class OverlapCancelTests(TestCase):
    """`overlap='cancel'` acts on everything `_is_busy` calls in flight."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='A')
        self.trigger = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            overlap='cancel',
        )

    def test_a_paused_run_is_cancelled_with_its_pending_approvals(self):
        from agents.sweep import _cancel_running

        log = ExecutionLog.objects.create(subagent=self.agent, user=self.user,
                                          status='paused')
        hitl = HITLRequest.objects.create(execution=log, user=self.user,
                                          request_type='approval', title='t',
                                          message='m', status='pending')

        self.assertEqual(_cancel_running(self.trigger), 1)

        log.refresh_from_db()
        hitl.refresh_from_db()
        self.assertEqual(log.status, 'cancelled')
        # Otherwise notifications/reminders.py keeps escalating it for ever.
        self.assertEqual(hitl.status, 'cancelled')


class CronWeekdayTests(TestCase):
    """1-7 is how people write "every day"."""

    def test_seven_is_sunday_inside_a_range(self):
        from agents.triggers import is_valid, parse_cron

        self.assertTrue(is_valid('0 9 * * 1-7'))
        self.assertEqual(parse_cron('0 9 * * 1-7')[4], {0, 1, 2, 3, 4, 5, 6})

    def test_seven_is_still_sunday_on_its_own(self):
        from agents.triggers import parse_cron

        self.assertEqual(parse_cron('0 9 * * 7')[4], {0})

    def test_eight_is_still_rejected(self):
        from agents.triggers import is_valid

        self.assertFalse(is_valid('0 9 * * 8'))


class ResumeDepthTests(TestCase):
    """A pause must not reset the delegation counter that bounds fan-out."""

    def test_resume_carries_the_depth_the_run_was_opened_at(self):
        from asgiref.sync import async_to_sync

        import agents.agent.runtime as runtime
        import workflow_backend.background as background

        user = User.objects.create_user(username='owner', password='pw')
        agent = SubAgent.objects.create(user=user, name='A')
        ExecutionLog.objects.create(
            subagent=agent, user=user, status='paused', depth=1,
            input_data={'goal': 'g', 'thread_id': 'th-1'},
        )

        seen: dict = {}

        async def _fake_run_agent(*args, **kwargs):
            seen.update(kwargs)

        spawned = []
        real_run_agent, real_spawn = runtime.run_agent, background.spawn
        runtime.run_agent = _fake_run_agent
        background.spawn = lambda coro, name='': spawned.append(coro)
        try:
            async_to_sync(runtime.resume_agent_run)(
                agent, user=user, thread_id='th-1')
            # `spawn` is stubbed, so drive the detached coroutine here.
            async_to_sync(lambda: spawned[0])()
        finally:
            runtime.run_agent = real_run_agent
            background.spawn = real_spawn

        self.assertEqual(seen.get('depth'), 1)
