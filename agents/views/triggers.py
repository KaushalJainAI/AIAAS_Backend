"""
Triggers: the ways something other than the user can start a run.

The webhook receiver is the one unauthenticated endpoint in this app, and what
it does is spend the owner's model credits on request, so it is built to
refuse by default:

- the URL carries a generated secret, never an agent id, so a run cannot be
  started by guessing a small integer;
- the agent must be `allow_unattended`, which is off on every row until someone
  turns it on — the runtime enforces this too, in `_check_unattended`, because
  a check that only exists in a view is a check that a later caller skips;
- the trigger must be enabled, and it disables itself after repeated failures;
- the payload is capped, and it is passed as *context*, never as the goal — an
  inbound request that could dictate the instruction would be prompt injection
  with a URL.
"""
from __future__ import annotations

import json
import logging

from adrf.decorators import api_view as async_api_view
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from agents.models import SubAgent, Trigger
from agents.triggers import (
    describe as cron_describe,
    is_valid as cron_is_valid,
    next_run_after,
    next_runs,
    zone_is_valid,
)

logger = logging.getLogger(__name__)

#: Inbound webhook bodies larger than this are refused. The body becomes model
#: context, so it is charged for by the token.
MAX_WEBHOOK_BODY_BYTES = 64 * 1024

#: How many upcoming firings a trigger carries in its own representation, and
#: the ceiling on what `/preview/` will compute. Small on purpose: the list is
#: there to confirm the user read the schedule the way the server does, and
#: `next_runs` walks minute by minute inside an hour.
UPCOMING_RUNS = 3
MAX_PREVIEW_RUNS = 10

#: `Nothing returns an unbounded list` — a user with a trigger per agent and a
#: hundred agents would otherwise serialize the lot, each with its own cron
#: walk. The body says when it has been cut.
TRIGGER_LIST_LIMIT = 200


class TriggerSerializer(serializers.ModelSerializer):
    """The wire shape of a trigger, plus the three things a schedule needs to
    be *checkable*: what it says in words, when it fires next, and what
    happened last time.

    `cron` is write-only into `config`; `schedule_cron` reads it back out. The
    round trip used to be asymmetric — a client had to write `cron` and read
    `config.cron` — which is the kind of seam a form binds to wrongly once and
    then silently stops saving.
    """

    cron = serializers.CharField(required=False, allow_blank=True, write_only=True)
    schedule_cron = serializers.CharField(source='cron', read_only=True)
    webhook_url = serializers.SerializerMethodField()
    agent_name = serializers.CharField(source='subagent.name', read_only=True)
    #: Whether the agent is cleared to run unattended. A schedule on an agent
    #: without it is refused at every firing, and until this was surfaced the
    #: only evidence was five failures and a self-disabled row.
    agent_allows_unattended = serializers.BooleanField(
        source='subagent.allow_unattended', read_only=True,
    )
    description = serializers.SerializerMethodField()
    upcoming = serializers.SerializerMethodField()

    class Meta:
        model = Trigger
        fields = (
            'id', 'subagent', 'agent_name', 'agent_allows_unattended', 'mode',
            'config', 'cron', 'schedule_cron', 'timezone', 'name', 'goal',
            'enabled', 'overlap', 'origin', 'starts_at', 'ends_at',
            'last_fired_at', 'next_due_at', 'queued_for', 'consecutive_failures',
            'last_outcome', 'last_error', 'description', 'upcoming',
            'webhook_url', 'created_at', 'updated_at',
        )
        read_only_fields = (
            'origin', 'last_fired_at', 'next_due_at', 'queued_for',
            'consecutive_failures', 'last_outcome', 'last_error',
            'created_at', 'updated_at',
        )

    def get_webhook_url(self, obj) -> str | None:
        """The secret is only ever shown through this field, to the owner."""
        if obj.mode != 'webhook' or not obj.secret:
            return None
        return f'/api/orchestrator/hooks/{obj.secret}/'

    def get_description(self, obj) -> str:
        """The cron expression in words. See `triggers.describe` for why."""
        return cron_describe(obj.cron, obj.tz) if obj.mode == 'schedule' else ''

    def get_upcoming(self, obj) -> list[str]:
        """The next few firings, so a listing shows the schedule's meaning and
        not just its syntax. Empty for a disabled or non-schedule trigger —
        showing times for something that will not fire is a lie the UI would
        have to undo."""
        if obj.mode != 'schedule' or not obj.enabled or not obj.cron:
            return []
        after = max(timezone.now(), obj.starts_at or timezone.now())
        runs = next_runs(obj.cron, after, obj.tz, count=UPCOMING_RUNS)
        if obj.ends_at:
            runs = [r for r in runs if r <= obj.ends_at]
        return [r.isoformat() for r in runs]

    def validate_timezone(self, value):
        value = (value or 'UTC').strip() or 'UTC'
        if not zone_is_valid(value):
            raise serializers.ValidationError(
                f'"{value}" is not an IANA timezone name, e.g. "Asia/Kolkata".'
            )
        return value

    def validate(self, attrs):
        mode = attrs.get('mode') or getattr(self.instance, 'mode', None)
        cron = (attrs.pop('cron', '') or '').strip()

        if mode == 'schedule':
            existing = (getattr(self.instance, 'config', None) or {}).get('cron', '')
            cron = cron or existing
            if not cron:
                raise serializers.ValidationError(
                    {'cron': 'A schedule trigger needs a cron expression, '
                             'otherwise it never fires.'}
                )
            if not cron_is_valid(cron):
                raise serializers.ValidationError(
                    {'cron': 'Expected five cron fields, e.g. "0 9 * * 1".'}
                )
            tz = attrs.get('timezone') or getattr(self.instance, 'tz', 'UTC')
            # A syntactically valid expression that never comes round — the
            # classic is 30 February — would otherwise be saved with a NULL
            # `next_due_at` and simply never fire, with nothing to look at.
            if next_run_after(cron, timezone.now(), tz) is None:
                raise serializers.ValidationError(
                    {'cron': f'"{cron}" has no next run — check the day and '
                             f'month fields.'}
                )
            attrs['config'] = dict(attrs.get('config') or {}, cron=cron)

        starts = attrs.get('starts_at', getattr(self.instance, 'starts_at', None))
        ends = attrs.get('ends_at', getattr(self.instance, 'ends_at', None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError(
                {'ends_at': 'The end of the window must be after its start.'}
            )
        return attrs

    def validate_subagent(self, value):
        if value.user_id != self.context['request'].user.id:
            raise serializers.ValidationError('No such agent.')
        return value


def _arm(trigger: Trigger) -> None:
    """Point a saved schedule at its first firing.

    Armed from `starts_at` when the window has not opened yet, so a schedule
    created today to begin next month is not repeatedly woken and re-armed by
    every sweep in between.
    """
    if trigger.mode != 'schedule':
        return
    now = timezone.now()
    after = max(now, trigger.starts_at) if trigger.starts_at else now
    trigger.next_due_at = next_run_after(trigger.cron, after, trigger.tz)
    trigger.queued_for = None
    trigger.save(update_fields=['next_due_at', 'queued_for', 'updated_at'])


@extend_schema(methods=['GET'], responses={200: TriggerSerializer(many=True)})
@extend_schema(methods=['POST'], request=TriggerSerializer, responses={201: TriggerSerializer})
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def trigger_list(request):
    """List or create the caller's triggers."""
    if request.method == 'GET':
        rows = (
            Trigger.objects
            .filter(subagent__user=request.user)
            .select_related('subagent')
            .order_by('-updated_at')
        )
        # ?agent=<id> so the builder can show one agent's schedules without
        # pulling every trigger the user owns.
        agent_id = request.query_params.get('agent')
        if agent_id:
            rows = rows.filter(subagent_id=agent_id)

        page = list(rows[:TRIGGER_LIST_LIMIT + 1])
        truncated = len(page) > TRIGGER_LIST_LIMIT
        data = TriggerSerializer(page[:TRIGGER_LIST_LIMIT], many=True).data
        if truncated:
            # A capped list and a complete one must not look alike.
            return Response({'results': data, 'truncated': True,
                             'limit': TRIGGER_LIST_LIMIT})
        return Response(data)

    serializer = TriggerSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)
    # `origin` is read-only over the wire: only `AgentSerializer.sync_schedule`
    # may claim the builder's row, or a client could take ownership of it and
    # have the next agent save silently overwrite its schedule.
    trigger = serializer.save(origin='manual')
    _arm(trigger)
    return Response(
        TriggerSerializer(trigger).data, status=status.HTTP_201_CREATED,
    )


@extend_schema(methods=['GET'], responses={200: TriggerSerializer})
@extend_schema(methods=['PATCH'], request=TriggerSerializer, responses={200: TriggerSerializer})
@extend_schema(methods=['DELETE'], responses={204: OpenApiResponse(description='Deleted')})
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def trigger_detail(request, trigger_id: int):
    trigger = get_object_or_404(
        Trigger, id=trigger_id, subagent__user=request.user,
    )

    if request.method == 'GET':
        return Response(TriggerSerializer(trigger).data)

    if request.method == 'DELETE':
        trigger.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = TriggerSerializer(
        trigger, data=request.data, partial=True, context={'request': request},
    )
    serializer.is_valid(raise_exception=True)
    trigger = serializer.save()
    # Re-enabling clears the failure count, otherwise a trigger that disabled
    # itself would fire once and disable again immediately.
    if trigger.enabled and trigger.consecutive_failures:
        trigger.consecutive_failures = 0
        trigger.save(update_fields=['consecutive_failures', 'updated_at'])
    _arm(trigger)
    return Response(TriggerSerializer(trigger).data)


class SchedulePreviewSerializer(serializers.Serializer):
    """What the editor asks before anything is saved."""

    cron = serializers.CharField()
    timezone = serializers.CharField(required=False, allow_blank=True, default='UTC')
    count = serializers.IntegerField(
        required=False, default=UPCOMING_RUNS, min_value=1, max_value=MAX_PREVIEW_RUNS,
    )
    starts_at = serializers.DateTimeField(required=False, allow_null=True)
    ends_at = serializers.DateTimeField(required=False, allow_null=True)


@extend_schema(
    methods=['POST'],
    request=SchedulePreviewSerializer,
    responses={200: OpenApiResponse(description='Reading and upcoming firings')},
    description='Validate a cron expression and say, in words and in dates, what it means.',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def schedule_preview(request):
    """Answer "what does this schedule actually do" before it is saved.

    Deliberately not a mirror of the client's own cron reader: the client can
    render a guess instantly, but the only reading that matters is the one the
    sweep will act on, and that is this code. A preview computed by a second
    implementation would agree right up until the day it did not.

    Answers 200 with `valid: false` for a bad expression rather than 400. The
    caller is a field the user is still typing in; a 400 per keystroke is an
    error report, not feedback.
    """
    form = SchedulePreviewSerializer(data=request.data)
    form.is_valid(raise_exception=True)
    data = form.validated_data

    cron = (data['cron'] or '').strip()
    tz = (data.get('timezone') or 'UTC').strip() or 'UTC'

    if not zone_is_valid(tz):
        return Response({
            'valid': False,
            'error': f'"{tz}" is not an IANA timezone name, e.g. "Asia/Kolkata".',
            'description': '', 'upcoming': [],
        })
    if not cron_is_valid(cron):
        return Response({
            'valid': False,
            'error': 'Expected five cron fields, e.g. "0 9 * * 1".',
            'description': '', 'upcoming': [],
        })

    now = timezone.now()
    starts_at = data.get('starts_at')
    runs = next_runs(cron, max(now, starts_at) if starts_at else now, tz,
                     count=data['count'])
    ends_at = data.get('ends_at')
    if ends_at:
        runs = [r for r in runs if r <= ends_at]

    if not runs:
        return Response({
            'valid': False,
            'error': ('This schedule has no next run. Check the day and month '
                      'fields — and the end date, if you set one.'),
            'description': cron_describe(cron, tz), 'upcoming': [],
        })

    return Response({
        'valid': True,
        'error': '',
        'description': cron_describe(cron, tz),
        'upcoming': [r.isoformat() for r in runs],
    })


@extend_schema(
    methods=['POST'],
    responses={200: OpenApiResponse(description='Outcome of the firing')},
    description='Fire a schedule trigger now, through the same path the sweep uses.',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def trigger_run_now(request, trigger_id: int):
    """Fire this trigger immediately, and report what happened.

    Deliberately `sweep.fire`, not a direct `start_agent_run`: a "test" button
    that took a shortcut past the overlap policy, the unattended gate and the
    failure counter would prove the button works and nothing else. The whole
    question a user has before trusting a schedule is whether *the scheduled
    path* runs, so this exercises exactly that path and hands back the same
    one-word outcome the sweep counts.

    Schedules only. A webhook trigger is already fireable by POSTing its URL,
    and an event trigger has no runtime yet; both are refused by name rather
    than silently doing nothing.
    """
    from agents.sweep import fire

    trigger = get_object_or_404(
        Trigger, id=trigger_id, subagent__user=request.user,
    )
    if trigger.mode != 'schedule':
        return Response(
            {'error': f'Only schedule triggers can be run from here; this one is '
                      f'"{trigger.mode}".'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not trigger.enabled:
        return Response(
            {'error': 'This trigger is disabled. Enable it first — that also '
                      'clears its failure count.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    outcome = fire(trigger)
    trigger.refresh_from_db()
    return Response({
        'outcome': outcome,
        'trigger': TriggerSerializer(trigger).data,
    })


@extend_schema(
    methods=['POST'],
    responses={202: OpenApiResponse(description='Run accepted')},
    description='Public webhook receiver. The secret in the path is the only credential.',
    auth=[],
)
@async_api_view(['POST'])
@permission_classes([AllowAny])
async def webhook_receive(request, secret: str):
    """
    Start a run from an inbound HTTP request.

    Answers 202 and nothing else on success — no agent name, no execution id.
    An unauthenticated caller learning which agent it just started, or getting
    an id it can subscribe to, is a disclosure the owner did not ask for.
    Failures answer 404 for the same reason: "wrong secret", "disabled" and
    "not cleared for unattended runs" must not be distinguishable from outside,
    or the endpoint becomes an oracle for probing which secrets are live.
    """
    from agents.agent.runtime import AgentRunRefused, start_agent_run

    if len(request.body or b'') > MAX_WEBHOOK_BODY_BYTES:
        return Response({'error': 'Payload too large.'},
                        status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    trigger = await (
        Trigger.objects
        .select_related('subagent', 'subagent__user')
        .filter(secret=secret, mode='webhook', enabled=True)
        .afirst()
    )
    if trigger is None:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    agent = trigger.subagent
    goal = (trigger.goal or agent.prompt or '').strip()
    if not goal:
        logger.warning('[Webhook] Trigger %s has nothing to ask the agent.', trigger.id)
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    # The body is context, never the instruction. A caller who could set the
    # goal could make the agent do anything its grants allow, from a URL that
    # by design has no authentication behind it.
    payload = _readable_payload(request)
    if payload:
        goal = f'{goal}\n\nInbound webhook payload:\n{payload}'

    try:
        await start_agent_run(
            agent, goal, user=agent.user, trigger_type='webhook',
            caller='trigger',
        )
    except AgentRunRefused as exc:
        # Includes the unattended refusal. Logged for the owner, opaque to the
        # caller.
        logger.warning('[Webhook] Trigger %s refused: %s', trigger.id, exc)
        await _note_failure(trigger)
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    except Exception:  # noqa: BLE001
        logger.exception('[Webhook] Trigger %s failed to start', trigger.id)
        await _note_failure(trigger)
        return Response({'error': 'Could not start the run.'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    await Trigger.objects.filter(id=trigger.id).aupdate(
        last_fired_at=timezone.now(), consecutive_failures=0,
    )
    return Response(status=status.HTTP_202_ACCEPTED)


async def _note_failure(trigger) -> None:
    """Count a refused or failed firing, and disable the trigger once it is
    clearly never going to work.

    The module docstring above has always promised "it disables itself after
    repeated failures", but only the schedule sweep ever counted: this endpoint
    reset `consecutive_failures` on success and incremented it nowhere, so a
    webhook pointed at an agent that is not cleared for unattended runs stayed
    live for ever, refusing every request. Uses the same ceiling as the sweep,
    because the two are the same policy on the same column.
    """
    from django.db.models import F

    from agents.sweep import MAX_CONSECUTIVE_FAILURES

    await Trigger.objects.filter(id=trigger.id).aupdate(
        consecutive_failures=F('consecutive_failures') + 1,
    )
    failures = await (
        Trigger.objects.filter(id=trigger.id)
        .values_list('consecutive_failures', flat=True)
        .afirst()
    )
    if failures is not None and failures >= MAX_CONSECUTIVE_FAILURES:
        await Trigger.objects.filter(id=trigger.id).aupdate(enabled=False)
        logger.error('[Webhook] Trigger %s disabled after %s consecutive failures.',
                     trigger.id, failures)


def _readable_payload(request) -> str:
    """The request body as something worth putting in a prompt."""
    try:
        body = (request.body or b'').decode('utf-8', errors='replace').strip()
    except Exception:  # noqa: BLE001
        return ''
    if not body:
        return ''
    try:
        return json.dumps(json.loads(body), indent=2)[:MAX_WEBHOOK_BODY_BYTES]
    except (json.JSONDecodeError, TypeError):
        return body[:MAX_WEBHOOK_BODY_BYTES]
