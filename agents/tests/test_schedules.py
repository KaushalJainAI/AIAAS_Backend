"""
Schedule configuration: timezones, windows, queueing, and the preview.

These are the parts of a schedule a user *configures*, as distinct from
`test_triggers.py`, which covers the cron arithmetic and the webhook receiver.
The split is deliberate — most of what is asserted here is that a schedule
means what the person who typed it thought it meant, which is a different
question from whether the parser is correct.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from agents.models import SubAgent, Trigger
from agents.triggers import describe, next_run_after, next_runs, zone_is_valid


def at(*args) -> datetime:
    return datetime(*args, tzinfo=dt_timezone.utc)


class TimezoneTests(SimpleTestCase):
    """A schedule is read in its own zone and stored as a UTC instant."""

    def test_omitting_the_zone_keeps_the_old_utc_behaviour(self):
        # The whole point of the default: existing rows must not move.
        self.assertEqual(
            next_run_after('0 9 * * *', at(2026, 8, 29, 12, 0)),
            at(2026, 8, 30, 9, 0),
        )

    def test_nine_in_the_morning_means_nine_where_the_owner_is(self):
        # IST is UTC+5:30, so 09:00 local is 03:30 UTC — which is the number
        # that goes in `next_due_at`, and the reason the column can stay UTC.
        self.assertEqual(
            next_run_after('0 9 * * *', at(2026, 8, 29, 12, 0), 'Asia/Kolkata'),
            at(2026, 8, 30, 3, 30),
        )

    def test_an_unknown_zone_falls_back_rather_than_raising(self):
        # A stored row naming a zone this machine's tzdata dropped must still
        # schedule something. An hour offset is visible; a trigger that stops
        # firing with no log line is not.
        self.assertEqual(
            next_run_after('0 9 * * *', at(2026, 8, 29, 12, 0), 'Mars/Olympus'),
            at(2026, 8, 30, 9, 0),
        )

    def test_zone_validation(self):
        self.assertTrue(zone_is_valid('Asia/Kolkata'))
        self.assertTrue(zone_is_valid('UTC'))
        self.assertFalse(zone_is_valid('Mars/Olympus'))
        self.assertFalse(zone_is_valid(''))


class DaylightSavingTests(SimpleTestCase):
    """The two hours a year that a naive scheduler gets wrong."""

    def test_a_time_that_does_not_exist_is_skipped(self):
        # 2027-03-14 02:30 never happens in New York: the clock goes 01:59 ->
        # 03:00. Firing at 03:30 instead would be an hour the user never asked
        # for, so the firing moves to the next day.
        runs = next_runs('30 2 * * *', at(2027, 3, 13, 12, 0),
                         'America/New_York', count=1)
        self.assertEqual(runs, [at(2027, 3, 15, 6, 30)])

    def test_a_time_that_happens_twice_fires_once(self):
        # 2026-11-01 01:30 occurs twice in New York. Both are valid instants;
        # firing on both would double-run a daily job once a year.
        runs = next_runs('30 1 * * *', at(2026, 10, 31, 12, 0),
                         'America/New_York', count=2)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0], at(2026, 11, 1, 5, 30))
        self.assertNotEqual(runs[0].date(), runs[1].date())

    def test_a_daily_schedule_holds_its_local_hour_across_the_change(self):
        runs = next_runs('0 9 * * *', at(2026, 10, 30, 12, 0),
                         'America/New_York', count=4)
        local_hours = {
            r.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).hour
            for r in runs
        }
        self.assertEqual(local_hours, {9})


class DescribeTests(SimpleTestCase):
    """`0 9 * * 1` and `9 0 * * 1` are both valid and nine hours apart.

    The expected strings here are also asserted, verbatim, by
    `src/lib/__tests__/cron.test.ts`. That duplication is the point: the client
    renders its own reading while the user types and this one replaces it when
    the preview lands, so a wording that differs by a single word shows up as
    the sentence rewriting itself under the cursor. Two tables that must agree
    is the cheapest way to notice when they stop.
    """

    #: Shape -> reading. Every one of these is a shape the schedule editor's
    #: pickers can produce, which is why the wordings are pinned.
    CANONICAL = {
        '* * * * *': 'Every minute',
        '*/15 * * * *': 'Every 15 minutes',
        '5 * * * *': 'Every hour at :05',
        '0 */4 * * *': 'Every 4 hours, at :00',
        '0 9 * * *': 'Every day at 09:00',
        '9 0 * * *': 'Every day at 00:09',
        '30 7 * * 1-5': 'Every weekday at 07:30',
        '0 18 * * 1,4': 'Every Monday and Thursday at 18:00',
        '0 9 * * 0,6': 'Every Saturday and Sunday at 09:00',
        '30 6 1 * *': 'On the 1st of every month at 06:30',
    }

    def test_the_shapes_the_pickers_produce_read_as_english(self):
        for cron, expected in self.CANONICAL.items():
            with self.subTest(cron=cron):
                self.assertEqual(describe(cron), expected)

    def test_an_interval_takes_no_day_clause(self):
        # "Every minute, every day" — the tail says nothing the head has not.
        for cron in ('* * * * *', '*/15 * * * *', '5 * * * *', '0 */4 * * *'):
            with self.subTest(cron=cron):
                self.assertNotIn('every day', describe(cron))

    def test_an_hour_step_is_read_as_a_step_not_as_its_expansion(self):
        # The user picked "every 4 hours"; reading it back as the six clock
        # times it expands to answers a question nobody asked, and disagrees
        # with what the picker itself says.
        text = describe('0 */4 * * *')
        self.assertEqual(text, 'Every 4 hours, at :00')
        self.assertNotIn('08:00', text)

    def test_a_wide_schedule_is_never_read_as_a_bare_count(self):
        # "Every day at 18 times a day" is not a sentence. A count says how
        # often it lands and nothing about when, which is the only thing the
        # reader is checking.
        for cron in ('0,30 9-17 * * 1-5', '30 */2 * * *', '0 8-18/2 * * 1-5'):
            with self.subTest(cron=cron):
                text = describe(cron)
                self.assertNotIn('times a day', text)
                self.assertNotIn('at at', text)
                self.assertNotRegex(text, r'at\s+\d+\s+times')

    def test_weekdays_are_listed_monday_first(self):
        # Cron numbers weeks from Sunday, so sorting by the raw value gives
        # "Sunday and Saturday", which reads like a mistake.
        self.assertEqual(describe('0 9 * * 0,6'),
                         'Every Saturday and Sunday at 09:00')
        self.assertEqual(describe('0 9 * * 6,0'),
                         'Every Saturday and Sunday at 09:00')

    def test_a_whole_hour_does_not_read_as_a_single_instant(self):
        # "Every minute of 09:00" says the opposite of what it means.
        self.assertEqual(describe('* 9 * * *'),
                         'Every minute between 09:00 and 09:59')

    def test_a_single_day_and_month_are_folded_into_a_date(self):
        self.assertEqual(describe('0 9 25 12 *'), 'On 25 December at 09:00')

    def test_the_zone_is_named_when_given(self):
        self.assertEqual(describe('0 9 * * *', 'Asia/Kolkata'),
                         'Every day at 09:00 (Asia/Kolkata)')

    def test_the_or_semantics_of_day_and_weekday_put_the_time_first(self):
        # Cron matches either field when both are restricted. Trailing the time
        # ("on the 13th, or every Friday at 09:00") reads as though 09:00
        # applies only to the Friday.
        text = describe('0 9 13 * 5')
        self.assertTrue(text.startswith('At 09:00,'), text)
        self.assertIn('13th', text)
        self.assertIn('Friday', text)
        self.assertIn(' or ', text)

    def test_an_unparseable_expression_describes_as_empty(self):
        # So a caller rendering a stored row never has to guard the call.
        self.assertEqual(describe('nonsense'), '')


class ScheduleWindowTests(TestCase):
    """`starts_at` / `ends_at`: a schedule that is not broken, just not live."""

    def setUp(self):
        self.user = User.objects.create_user(username='windower', password='pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Windowed', prompt='Do the thing.',
            allow_unattended=True, llm_provider='nvidia', llm_model='m',
        )

    def _trigger(self, **kw):
        defaults = dict(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            goal='Do the thing.', next_due_at=timezone.now() - timedelta(minutes=1),
        )
        return Trigger.objects.create(**{**defaults, **kw})

    def _sweep(self):
        started = []

        async def fake_start(agent, goal, **kwargs):
            started.append(goal)
            return 'exec-1'

        from agents import sweep
        with patch('agents.agent.runtime.start_agent_run', fake_start):
            counts = sweep.run_trigger_sweep()
        return counts, started

    def test_a_schedule_that_has_not_started_waits_rather_than_fires(self):
        t = self._trigger(starts_at=timezone.now() + timedelta(days=30))
        counts, started = self._sweep()
        self.assertEqual(counts, {'waiting': 1})
        self.assertEqual(started, [])
        t.refresh_from_db()
        self.assertEqual(t.last_outcome, 'waiting')
        self.assertTrue(t.enabled)
        # Armed past its start date, so the sweep is not woken by it daily
        # between now and then.
        self.assertGreater(t.next_due_at, t.starts_at)

    def test_a_schedule_past_its_end_closes_itself_and_says_so(self):
        t = self._trigger(ends_at=timezone.now() - timedelta(days=1))
        counts, started = self._sweep()
        self.assertEqual(counts, {'expired': 1})
        self.assertEqual(started, [])
        t.refresh_from_db()
        self.assertFalse(t.enabled)
        self.assertEqual(t.last_outcome, 'expired')
        self.assertIn('end date', t.last_error)

    def test_a_schedule_inside_its_window_fires_normally(self):
        self._trigger(starts_at=timezone.now() - timedelta(days=1),
                      ends_at=timezone.now() + timedelta(days=30))
        counts, started = self._sweep()
        self.assertEqual(counts, {'fired': 1})
        self.assertEqual(len(started), 1)

    def test_an_impossible_cron_disables_the_row_with_a_reason(self):
        # Rather than parking `next_due_at` at NULL for ever, which is
        # indistinguishable from a working schedule in every listing.
        t = self._trigger(config={'cron': '0 0 30 2 *'})
        self._sweep()
        t.refresh_from_db()
        self.assertFalse(t.enabled)
        self.assertEqual(t.last_outcome, 'stopped')
        self.assertIn('no next run', t.last_error)


class OverlapQueueTests(TestCase):
    """`overlap='queue'` was offered by the UI and implemented nowhere."""

    def setUp(self):
        self.user = User.objects.create_user(username='queuer', password='pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Queued', prompt='Do the thing.',
            allow_unattended=True, llm_provider='nvidia', llm_model='m',
        )
        self.trigger = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            goal='Do the thing.', overlap='queue',
            next_due_at=timezone.now() - timedelta(minutes=1),
        )

    def _busy(self):
        from logs.models import ExecutionLog
        return ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running',
            trigger_type='schedule',
        )

    def _sweep(self):
        started = []

        async def fake_start(agent, goal, **kwargs):
            started.append(goal)
            return 'exec-1'

        from agents import sweep
        with patch('agents.agent.runtime.start_agent_run', fake_start):
            counts = sweep.run_trigger_sweep()
        return counts, started

    def test_a_busy_agent_defers_the_firing_instead_of_running_it(self):
        run = self._busy()
        counts, started = self._sweep()
        self.assertEqual(counts, {'queued': 1})
        self.assertEqual(started, [], 'queue must not run immediately')
        self.trigger.refresh_from_db()
        self.assertIsNotNone(self.trigger.queued_for)
        self.assertEqual(self.trigger.last_outcome, 'queued')
        # And the next slot is armed as well, so the queue does not replace
        # the schedule.
        self.assertIsNotNone(self.trigger.next_due_at)
        self.assertEqual(run.status, 'running')

    def test_the_owed_firing_runs_once_the_agent_is_free(self):
        run = self._busy()
        self._sweep()
        run.status = 'completed'
        run.save(update_fields=['status'])

        counts, started = self._sweep()
        self.assertEqual(counts, {'fired': 1})
        self.assertEqual(len(started), 1)
        self.trigger.refresh_from_db()
        self.assertIsNone(self.trigger.queued_for)

    def test_a_stale_queued_firing_is_dropped_rather_than_delivered_late(self):
        from agents.sweep import QUEUE_TTL_SECONDS

        self._busy()
        self.trigger.queued_for = timezone.now() - timedelta(
            seconds=QUEUE_TTL_SECONDS + 60)
        self.trigger.next_due_at = timezone.now() + timedelta(days=1)
        self.trigger.save(update_fields=['queued_for', 'next_due_at'])

        counts, started = self._sweep()
        self.assertEqual(counts, {'dropped': 1})
        self.assertEqual(started, [], 'a 09:00 report is not wanted at midnight')
        self.trigger.refresh_from_db()
        self.assertIsNone(self.trigger.queued_for)
        # And tomorrow's slot is left alone: dropping a stale firing must not
        # bring the next one forward.
        self.assertGreater(self.trigger.next_due_at, timezone.now())

    def test_skip_still_skips(self):
        self.trigger.overlap = 'skip'
        self.trigger.save(update_fields=['overlap'])
        self._busy()
        counts, started = self._sweep()
        self.assertEqual(counts, {'busy': 1})
        self.assertEqual(started, [])
        self.trigger.refresh_from_db()
        self.assertIsNone(self.trigger.queued_for)


class FailureReasonTests(TestCase):
    """The reason a firing was refused used to exist only in a server log."""

    def setUp(self):
        self.user = User.objects.create_user(username='failer', password='pw')
        # No `allow_unattended`, so every firing is refused by the runtime.
        self.agent = SubAgent.objects.create(
            user=self.user, name='Refused', prompt='Do the thing.',
            llm_provider='nvidia', llm_model='m',
        )
        self.trigger = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            goal='Do the thing.',
            next_due_at=timezone.now() - timedelta(minutes=1),
        )

    def test_a_refusal_is_stored_where_the_owner_can_read_it(self):
        from agents import sweep
        outcome = sweep.fire(self.trigger)
        self.assertEqual(outcome, 'refused')
        self.trigger.refresh_from_db()
        self.assertEqual(self.trigger.last_outcome, 'refused')
        self.assertIn('unattended', self.trigger.last_error)


class SchedulePreviewTests(APITestCase):
    """The endpoint the editor calls before anything is saved."""

    def setUp(self):
        self.user = User.objects.create_user(username='previewer', password='pw')
        self.client.force_authenticate(self.user)
        self.url = reverse('orchestrator:schedule_preview')

    def test_a_valid_schedule_comes_back_in_words_and_in_dates(self):
        res = self.client.post(self.url, {
            'cron': '0 9 * * 1-5', 'timezone': 'Asia/Kolkata', 'count': 3,
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['valid'])
        self.assertEqual(res.data['description'],
                         'Every weekday at 09:00 (Asia/Kolkata)')
        self.assertEqual(len(res.data['upcoming']), 3)

    def test_a_bad_expression_answers_200_with_valid_false(self):
        # 400 per keystroke is an error report, not feedback.
        res = self.client.post(self.url, {'cron': 'nonsense'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['valid'])
        self.assertIn('five cron fields', res.data['error'])

    def test_a_schedule_that_never_comes_round_is_reported_as_invalid(self):
        res = self.client.post(self.url, {'cron': '0 0 30 2 *'}, format='json')
        self.assertFalse(res.data['valid'])
        self.assertIn('no next run', res.data['error'])

    def test_an_unknown_zone_is_reported_rather_than_silently_ignored(self):
        res = self.client.post(self.url, {
            'cron': '0 9 * * *', 'timezone': 'Mars/Olympus',
        }, format='json')
        self.assertFalse(res.data['valid'])
        self.assertIn('IANA', res.data['error'])

    def test_the_window_narrows_the_preview(self):
        res = self.client.post(self.url, {
            'cron': '0 9 * * *', 'count': 5,
            'ends_at': (timezone.now() + timedelta(days=2)).isoformat(),
        }, format='json')
        self.assertTrue(res.data['valid'])
        self.assertLessEqual(len(res.data['upcoming']), 2)

    def test_it_is_not_public(self):
        self.client.force_authenticate(None)
        res = self.client.post(self.url, {'cron': '0 9 * * *'}, format='json')
        self.assertIn(res.status_code, (401, 403))


class TriggerRepresentationTests(APITestCase):
    """What a schedule looks like on the wire, now that it has to be checkable."""

    def setUp(self):
        self.user = User.objects.create_user(username='reader', password='pw')
        self.client.force_authenticate(self.user)
        self.agent = SubAgent.objects.create(
            user=self.user, name='Reader', prompt='Do the thing.',
            allow_unattended=True, llm_provider='nvidia', llm_model='m',
        )

    def test_a_created_schedule_carries_its_reading_and_next_firings(self):
        res = self.client.post(reverse('orchestrator:trigger_list'), {
            'subagent': self.agent.id, 'mode': 'schedule',
            'cron': '0 9 * * 1-5', 'timezone': 'Asia/Kolkata',
            'name': 'Weekday briefing',
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['description'],
                         'Every weekday at 09:00 (Asia/Kolkata)')
        self.assertEqual(res.data['schedule_cron'], '0 9 * * 1-5')
        self.assertEqual(len(res.data['upcoming']), 3)
        self.assertTrue(res.data['agent_allows_unattended'])
        # Created through the API, so the builder's field does not own it.
        self.assertEqual(res.data['origin'], 'manual')

    def test_an_impossible_schedule_is_refused_at_the_door(self):
        res = self.client.post(reverse('orchestrator:trigger_list'), {
            'subagent': self.agent.id, 'mode': 'schedule', 'cron': '0 0 30 2 *',
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('no next run', str(res.data['cron']))

    def test_an_unknown_zone_is_refused(self):
        res = self.client.post(reverse('orchestrator:trigger_list'), {
            'subagent': self.agent.id, 'mode': 'schedule', 'cron': '0 9 * * *',
            'timezone': 'Mars/Olympus',
        }, format='json')
        self.assertEqual(res.status_code, 400)

    def test_a_backwards_window_is_refused(self):
        now = timezone.now()
        res = self.client.post(reverse('orchestrator:trigger_list'), {
            'subagent': self.agent.id, 'mode': 'schedule', 'cron': '0 9 * * *',
            'starts_at': (now + timedelta(days=2)).isoformat(),
            'ends_at': now.isoformat(),
        }, format='json')
        self.assertEqual(res.status_code, 400)

    def test_a_disabled_schedule_shows_no_upcoming_firings(self):
        t = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            enabled=False,
        )
        res = self.client.get(
            reverse('orchestrator:trigger_detail', args=[t.id]))
        self.assertEqual(res.data['upcoming'], [])

    def test_the_timezone_is_honoured_when_arming(self):
        res = self.client.post(reverse('orchestrator:trigger_list'), {
            'subagent': self.agent.id, 'mode': 'schedule', 'cron': '0 9 * * *',
            'timezone': 'Asia/Kolkata',
        }, format='json')
        due = Trigger.objects.get(id=res.data['id']).next_due_at
        self.assertEqual((due.hour, due.minute), (3, 30))


class BuilderScheduleOwnershipTests(APITestCase):
    """One field cannot be the source of truth for a list of schedules."""

    def setUp(self):
        self.user = User.objects.create_user(username='builder', password='pw')
        self.client.force_authenticate(self.user)
        self.agent = SubAgent.objects.create(
            user=self.user, name='Builder', prompt='Do the thing.',
            allow_unattended=True, llm_provider='nvidia', llm_model='m',
        )

    def _save_agent(self, **overrides):
        from agents.views.agents import AgentSerializer

        payload = {
            'name': self.agent.name, 'brief': self.agent.prompt,
            'allowUnattended': True, **overrides,
        }
        request = type('R', (), {'user': self.user})()
        s = AgentSerializer(data=payload, context={'request': request})
        s.is_valid(raise_exception=True)
        AgentSerializer.apply(self.agent, s.validated_data)
        self.agent.save()
        AgentSerializer.sync_schedule(self.agent, s.validated_data)

    def test_saving_the_agent_does_not_touch_a_manually_added_schedule(self):
        manual = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 18 * * 5'},
            origin='manual', name='Friday wrap-up',
        )
        self._save_agent(schedule='0 9 * * 1-5', scheduleTimezone='Asia/Kolkata')

        manual.refresh_from_db()
        self.assertEqual(manual.cron, '0 18 * * 5')
        self.assertEqual(self.agent.triggers.filter(mode='schedule').count(), 2)

    def test_clearing_the_builder_field_removes_only_the_builder_row(self):
        Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 18 * * 5'},
            origin='manual',
        )
        self._save_agent(schedule='0 9 * * 1-5', scheduleTimezone='UTC')
        self._save_agent(schedule='')

        remaining = list(self.agent.triggers.filter(mode='schedule'))
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].origin, 'manual')

    def test_the_builder_field_shows_only_its_own_row(self):
        from agents.views.agents import AgentSerializer

        Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 6 * * *'},
            origin='manual',
        )
        config = AgentSerializer.to_config(self.agent)
        # Not '0 6 * * *': showing a manual schedule in a field that overwrites
        # on save is how the builder would eat it.
        self.assertEqual(config['schedule'], '')
        self.assertEqual(config['extraSchedules'], 1)

    def test_the_migration_marks_pre_existing_schedules_as_the_builders(self):
        # Until 0020, `sync_schedule` was the only writer of a schedule row, so
        # every one that already exists is the builder's. The backfill is what
        # lets `sync_schedule` have a rule rather than a read-time guess.
        import importlib

        migration = importlib.import_module(
            'agents.migrations.0020_trigger_schedule_config')
        legacy = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 6 * * *'},
        )
        Trigger.objects.filter(id=legacy.id).update(origin='manual')
        migration.mark_existing_as_builder(apps, None)
        legacy.refresh_from_db()
        self.assertEqual(legacy.origin, 'builder')

    def test_the_builder_timezone_round_trips(self):
        from agents.views.agents import AgentSerializer

        self._save_agent(schedule='0 9 * * *', scheduleTimezone='Asia/Kolkata')
        config = AgentSerializer.to_config(self.agent)
        self.assertEqual(config['schedule'], '0 9 * * *')
        self.assertEqual(config['scheduleTimezone'], 'Asia/Kolkata')
