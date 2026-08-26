"""
The 0015 backfill, exercised against the schema it actually runs on.

This is the one migration in the set that touches data, and it is the only thing
standing between an existing install and every historical run losing its trace
grouping: before `AgentTurn`, the turn a step belonged to lived in
`AgentStep.config['iteration']`, and 0016 drops that column.

It cannot be tested through the ORM, because the models no longer have `config`.
So the migration executor is driven directly: rewind to 0014, insert rows in the
old shape, run 0015, and check what came out.

These tests are slow (they migrate a database twice) but this is a migration
that runs once, on real data, with no undo.
"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BackfillTurnsMigrationTests(TransactionTestCase):
    """Drives `logs.0015` over rows written in the pre-turn shape."""

    #: The migration under test needs the apps it depends on to exist.
    available_apps = None

    before = [('logs', '0014_add_turns_and_revisions')]
    after = [('logs', '0016_drop_step_config')]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        executor.loader.build_graph()
        return executor.loader.project_state(targets).apps

    def setUp(self):
        self.apps_before = self._migrate(self.before)

    def tearDown(self):
        # Leave the database at the latest state so later tests are unaffected.
        self._migrate([('logs', '0016_drop_step_config')])

    def _seed(self, apps):
        User = apps.get_model('auth', 'User')
        SubAgent = apps.get_model('orchestrator', 'SubAgent')
        ExecutionLog = apps.get_model('logs', 'ExecutionLog')
        AgentStep = apps.get_model('logs', 'AgentStep')

        user = User.objects.create(username='historic')
        agent = SubAgent.objects.create(user=user, name='Old Agent')
        run = ExecutionLog.objects.create(
            user=user, subagent=agent, status='completed',
        )
        return run, AgentStep

    def test_steps_are_grouped_into_the_turns_that_issued_them(self):
        run, AgentStep = self._seed(self.apps_before)

        # Two calls issued together, then one on its own — exactly what
        # `config['iteration']` used to encode.
        AgentStep.objects.create(
            execution=run, call_id='c1', tool='web_search', status='completed',
            order=1, config={'iteration': 1, 'thought': 'Search two ways.'},
        )
        AgentStep.objects.create(
            execution=run, call_id='c2', tool='read_url', status='completed',
            order=2, config={'iteration': 1, 'thought': 'Search two ways.'},
        )
        AgentStep.objects.create(
            execution=run, call_id='c3', tool='execute_python',
            status='completed', order=3,
            config={'iteration': 2, 'thought': 'Now compute.'},
        )

        apps = self._migrate([('logs', '0015_backfill_turns_from_config')])
        AgentTurn = apps.get_model('logs', 'AgentTurn')
        Step = apps.get_model('logs', 'AgentStep')

        turns = list(AgentTurn.objects.filter(execution_id=run.id).order_by('index'))
        self.assertEqual([t.index for t in turns], [1, 2])
        self.assertEqual(turns[0].reasoning, 'Search two ways.')
        self.assertEqual(turns[1].reasoning, 'Now compute.')

        # The parallel pair stays a pair. Splitting them would turn a fan-out
        # into a chain and claim a causality the run never had.
        first = sorted(
            Step.objects.filter(turn_id=turns[0].id).values_list('call_id', flat=True)
        )
        self.assertEqual(first, ['c1', 'c2'])
        self.assertEqual(
            list(Step.objects.filter(turn_id=turns[1].id)
                 .values_list('call_id', flat=True)),
            ['c3'],
        )

    def test_recovered_reasoning_is_marked_as_truncated(self):
        """It was cut — by the old writer, at 150 characters. A short thought
        and a trimmed one must not look alike afterwards."""
        run, AgentStep = self._seed(self.apps_before)
        AgentStep.objects.create(
            execution=run, call_id='c1', tool='web_search', status='completed',
            order=1, config={'iteration': 1, 'thought': 'A clipped thought'},
        )

        apps = self._migrate([('logs', '0015_backfill_turns_from_config')])
        turn = apps.get_model('logs', 'AgentTurn').objects.get(execution_id=run.id)
        self.assertTrue(turn.reasoning_truncated)

    def test_a_turn_with_no_recorded_thought_is_not_marked_truncated(self):
        run, AgentStep = self._seed(self.apps_before)
        AgentStep.objects.create(
            execution=run, call_id='c1', tool='web_search', status='completed',
            order=1, config={'iteration': 1},
        )

        apps = self._migrate([('logs', '0015_backfill_turns_from_config')])
        turn = apps.get_model('logs', 'AgentTurn').objects.get(execution_id=run.id)
        self.assertEqual(turn.reasoning, '')
        self.assertFalse(turn.reasoning_truncated)

    def test_rows_predating_the_iteration_key_get_one_turn_each(self):
        """The same fallback the canvas projection used, so nothing renders
        differently than it did before the migration."""
        run, AgentStep = self._seed(self.apps_before)
        AgentStep.objects.create(
            execution=run, call_id='old1', tool='web_search',
            status='completed', order=1, config={},
        )
        AgentStep.objects.create(
            execution=run, call_id='old2', tool='read_url',
            status='completed', order=2, config={},
        )

        apps = self._migrate([('logs', '0015_backfill_turns_from_config')])
        AgentTurn = apps.get_model('logs', 'AgentTurn')
        self.assertEqual(
            list(AgentTurn.objects.filter(execution_id=run.id)
                 .order_by('index').values_list('index', flat=True)),
            [1, 2],
        )

    def test_the_last_turn_records_how_the_run_actually_ended(self):
        run, AgentStep = self._seed(self.apps_before)
        AgentStep.objects.create(
            execution=run, call_id='c1', tool='web_search', status='completed',
            order=1, config={'iteration': 1},
        )
        AgentStep.objects.create(
            execution=run, call_id='c2', tool='read_url', status='completed',
            order=2, config={'iteration': 2},
        )

        apps = self._migrate([('logs', '0015_backfill_turns_from_config')])
        AgentTurn = apps.get_model('logs', 'AgentTurn')
        turns = list(AgentTurn.objects.filter(execution_id=run.id).order_by('index'))
        # The run completed, so its final turn answered rather than calling more.
        self.assertEqual(turns[0].decision, 'tools')
        self.assertEqual(turns[1].decision, 'answer')

    def test_a_run_with_no_steps_is_left_alone(self):
        run, _ = self._seed(self.apps_before)

        apps = self._migrate([('logs', '0015_backfill_turns_from_config')])
        AgentTurn = apps.get_model('logs', 'AgentTurn')
        self.assertEqual(AgentTurn.objects.filter(execution_id=run.id).count(), 0)
