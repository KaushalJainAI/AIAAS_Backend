"""
Agents API.

An agent is a `SubAgent` row.

This module owns the translation between the frontend's `AgentConfig` (camelCase,
flat) and the model's columns (snake_case, grouped by who reads them). That
translation lives in exactly one place on purpose: the permissions screen an
installer sees is rendered from `tool_grants` / `guardrails` / `requirements`,
and the runtime enforces from those same columns. If a second mapping existed
the screen could promise something the runtime never checks.

Everything here is scoped to `request.user`. Ids that point at other rows
(knowledge bases, skills) are re-checked against the caller — an agent that
could attach someone else's knowledge base would be a cross-tenant read.

What is *not* here: starting or intervening in a run (`views/runs.py`) and the
read-only canvas projections (`views/canvas.py`). Those are a run's lifecycle;
this is an agent's configuration, and the two change for different reasons.
"""
import logging

from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from inference.models import KnowledgeBase
from logs import revisions
from logs.models import ExecutionLog
from skills.models import Skill

from django.utils import timezone

from agents import budget
from agents.models import SubAgent
from agents.serializers import SUSPICIOUS_WORKFLOW_NAME_RE
from agents.spend import rupees_for
from workflow_backend.thresholds import (
    DEFAULT_RUN_SECONDS,
    MAX_RUN_SECONDS,
    MIN_RUN_SECONDS,
)
from agents.triggers import (
    is_valid as cron_is_valid,
    next_run_after,
    zone_is_valid,
)

logger = logging.getLogger(__name__)


# The closed sets. Anything outside them is rejected rather than stored, because
# an unknown tool key would sail through the permissions screen (which renders
# what it knows) and land in the runtime unrecognised.
# 'mcp' unlocks the user's own configured MCP servers. It is a grant rather
# than an always-on capability because those tools reach real systems under
# the user's credentials — see mcp_integration/credential_injector.py.
TOOL_KEYS = {'codeExecution', 'shell', 'webSearch', 'scrape', 'fileOps', 'rag', 'mcp',
             'subAgents'}
# `connectors` holds `MCPServer` ids, validated against what the user can
# actually see. It used to be a hardcoded set of six presentation slugs
# ({'gdrive', 'gmail', 'sheets', 'photos', 'calendar', 'slack'}) that nothing
# read: it had drifted from the connector catalogue in both directions —
# `photos` names no server that has ever existed, while Notion and Fetch could
# not be named at all — and the runtime resolved the whole of `mcp` regardless.
# A closed list in code cannot work here for the same reason MCP tool names
# cannot be allow-listed by name: a user's own server is a row, minted after
# this file was written.
# Two axes — what may be read, what may be written — collapsed into one field.
# `read_all_write_own` is the combination the other four cannot express: read
# the user's whole tree, write only the agent's own home. See inference/vfs.py.
FILE_ACCESS = {'none', 'readonly', 'scoped', 'read_all_write_own', 'full'}
TRIGGER_MODES = {'goal', 'maintenance', 'template'}
# The autonomy ladder, loosest last. `plan` withholds every tool that could
# change anything, so a run can only look and report; `auto` stops asking about
# effects the user can undo (a file write lands in their recycle bin) while
# still stopping on the ones they cannot. The runtime resolves each level into
# a gate set and a policy — see `agents/agent/runtime.py::AUTONOMY_LADDER`,
# which is the authority on what each one means.
AUTONOMY = {'plan', 'review', 'ask', 'auto', 'full'}
EGRESS = {'none', 'allowlist', 'full'}

# What a single run may reserve. `cpu` and `memoryMb` used to live here and are
# gone (2026-08-29): both were stored, validated, round-tripped to the builder
# and read by nothing, and the panel said so in an orange "COMING SOON" badge.
# Neither could have been honoured — `sandbox/safe_execution.py` runs user code
# on a worker thread inside this process, where there is no cgroup to hang a CPU
# quota off and no way to cap one thread's RSS — so they were a promise the
# architecture could not keep. What a run actually contends for is wall-clock
# time, and `maxRunSeconds` is that, enforced in `agents/budget.py`.

# Autonomy is the agent-level word for the supervision vocabulary the HITL
# path already reads. Map rather than duplicate, so that path keeps working
# for agents unchanged.
AUTONOMY_TO_SUPERVISION = {
    'plan': 'full',
    'review': 'full',
    'ask': 'error_only',
    'auto': 'error_only',
    'full': 'none',
}
# Written out rather than derived by inverting the map above. Five autonomy
# levels do not fit into three supervision values, so an inversion would pick
# whichever key happened to be inserted last and call it the answer. These are
# the deliberate representatives: the loosest level that still means each
# supervision setting.
SUPERVISION_TO_AUTONOMY = {'none': 'full', 'error_only': 'ask', 'full': 'review'}


class AgentSerializer(serializers.Serializer):
    """The knob board, as a wire contract.

    Not a ModelSerializer: the frontend shape is flat and camelCase while the
    model groups fields by which subsystem reads them, so every field needs
    mapping anyway and a ModelSerializer would only obscure that.
    """

    id = serializers.IntegerField(read_only=True)

    # Identity
    name = serializers.CharField(max_length=200)
    brief = serializers.CharField(required=False, allow_blank=True, default='')

    # Model
    provider = serializers.CharField(max_length=30, required=False, default='openrouter')
    model = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    temperature = serializers.FloatField(required=False, default=0.2, min_value=0, max_value=2)

    # Sandbox
    fileAccess = serializers.ChoiceField(choices=sorted(FILE_ACCESS), required=False, default='scoped')
    workdir = serializers.CharField(max_length=255, required=False, default='/workspace')
    venv = serializers.BooleanField(required=False, default=True)

    # Tools
    tools = serializers.DictField(child=serializers.BooleanField(), required=False)

    # Context it is given
    # Which connections this agent may reach — `MCPServer` ids, the second axis
    # to the `mcp` grant just as `fileAccess` is to `fileOps`. Empty means "any
    # the user has", which is what every agent saved before this was enforced
    # has; see `agents.agent.runtime.mcp_scope_for`.
    connectors = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    knowledgeBases = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    skills = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    useOrgContext = serializers.BooleanField(required=False, default=True)
    useEnvironment = serializers.BooleanField(required=False, default=False)

    # Invocation
    trigger = serializers.ChoiceField(choices=sorted(TRIGGER_MODES), required=False, default='goal')
    schedule = serializers.CharField(required=False, allow_blank=True, default='')
    # The zone the cron fields are read in. Defaults to UTC so an agent saved
    # by an older client keeps firing at exactly the instant it always did —
    # this is a widening, and a widening that moves existing schedules by five
    # and a half hours is not one.
    scheduleTimezone = serializers.CharField(
        required=False, allow_blank=True, default='UTC',
    )
    # The gate every unattended caller is checked against. Declared here because
    # `apply` reads `validated_data`, so an undeclared field is silently dropped:
    # `to_config` emitted `allowUnattended`, nothing could ever set it, and a
    # scheduled agent therefore armed a Trigger that the runtime then refused
    # five times before the sweep disabled it. Defaults False on purpose — a
    # request that does not mention it is not a request to widen the agent.
    allowUnattended = serializers.BooleanField(required=False, default=False)

    # Guardrails
    autonomy = serializers.ChoiceField(choices=sorted(AUTONOMY), required=False, default='ask')
    notifyOnHitl = serializers.BooleanField(required=False, default=True)
    reviewAgent = serializers.BooleanField(required=False, default=False)
    spendCapRupees = serializers.IntegerField(required=False, default=500, min_value=0)
    # The other half of the blast radius, and the one that was missing. The
    # spend cap is monthly and per agent: it says nothing about how long a
    # single run may hold an event-loop slot, a checkpointer and a DB
    # connection, which is what actually decides whether everyone else's runs
    # get served. Seconds because that is the unit the runtime compares
    # against; the builder shows minutes.
    maxRunSeconds = serializers.IntegerField(
        required=False, default=DEFAULT_RUN_SECONDS,
        min_value=MIN_RUN_SECONDS, max_value=MAX_RUN_SECONDS,
    )
    # The knob the design note flagged as missing: an agent that can run code but
    # cannot reach the network is a very different thing to grant a stranger.
    egress = serializers.ChoiceField(choices=sorted(EGRESS), required=False, default='none')

    # Context lifecycle
    #: Which model folds the run's earlier steps when the window fills. Empty
    #: means the platform default (`CONTEXT_SUMMARY_MODEL`), which is a small
    #: NVIDIA model the platform holds a key for — so the fold works for a user
    #: who has connected nothing. Free-form rather than a choice field for the
    #: same reason `model` is: the catalogue lives in `AIModel` and a hardcoded
    #: enum here would go stale the first time a provider retires a name.
    summaryModel = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=200
    )
    summaryProvider = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=30
    )
    recursiveContext = serializers.BooleanField(required=False, default=True)
    compaction = serializers.BooleanField(required=False, default=True)
    indexing = serializers.BooleanField(required=False, default=True)

    # Read-only: observed behaviour, computed from the ledger of runs.
    status = serializers.CharField(read_only=True)
    runs = serializers.IntegerField(read_only=True)
    unattended = serializers.IntegerField(read_only=True)
    spend = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    # ---------------- validation ----------------

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError('Agent name cannot be blank.')
        if SUSPICIOUS_WORKFLOW_NAME_RE.search(name):
            raise serializers.ValidationError('Agent name contains unsupported characters or terms.')
        return name

    def validate_tools(self, value):
        unknown = set(value) - TOOL_KEYS
        if unknown:
            raise serializers.ValidationError(
                f"Unknown tools: {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(TOOL_KEYS))}."
            )
        return value

    def validate_connectors(self, value):
        """Reject any connection the caller cannot see.

        The mirror of `_owned_ids`, but it cannot use it: a curated connector is
        a row with `user IS NULL` that everyone can see, so filtering on
        `user=request.user` would reject every connection the platform ships and
        accept only the user's own. `visible_server_ids_sync` is the same
        predicate the runtime resolves through, which is the point — a selection
        the builder accepts has to be one the toolbox will honour.
        """
        from mcp_integration.client import visible_server_ids_sync

        ids = sorted(set(value))
        if not ids:
            return []
        unknown = set(ids) - visible_server_ids_sync(self.context['request'].user.id)
        if unknown:
            raise serializers.ValidationError(
                f"Unknown connections: {', '.join(str(i) for i in sorted(unknown))}."
            )
        return ids

    def validate_workdir(self, value):
        # The workdir is handed to the sandbox as a path. Traversal out of it
        # would defeat the file-access setting entirely.
        if not value.startswith('/') or '..' in value:
            raise serializers.ValidationError('workdir must be an absolute path without "..".')
        return value

    def validate_knowledgeBases(self, value):
        return self._owned_ids(KnowledgeBase, value, 'knowledge base')

    def validate_skills(self, value):
        return self._owned_ids(Skill, value, 'skill')

    def _owned_ids(self, model, ids, label):
        """Reject any id the caller does not own.

        Without this an agent could name someone else's knowledge base and read
        it on every run — the request is authenticated, so nothing downstream
        would look twice.
        """
        ids = sorted(set(ids))
        if not ids:
            return []
        user = self.context['request'].user
        owned = set(model.objects.filter(user=user, id__in=ids).values_list('id', flat=True))
        missing = [i for i in ids if i not in owned]
        if missing:
            raise serializers.ValidationError(
                f"No such {label}: {', '.join(map(str, missing))}."
            )
        return ids

    def validate(self, attrs):
        # A maintenance agent with no schedule never runs; saying so now beats a
        # silent no-op the user discovers a week later.
        trigger = attrs.get('trigger', 'goal')
        schedule = (attrs.get('schedule') or '').strip()
        tz = (attrs.get('scheduleTimezone') or 'UTC').strip() or 'UTC'
        if not zone_is_valid(tz):
            raise serializers.ValidationError(
                {'scheduleTimezone': f'"{tz}" is not an IANA timezone name, '
                                     f'e.g. "Asia/Kolkata".'}
            )
        attrs['scheduleTimezone'] = tz

        if trigger == 'maintenance':
            if not schedule:
                raise serializers.ValidationError(
                    {'schedule': 'A maintenance agent needs a schedule, otherwise it never runs.'}
                )
        if schedule:
            if not cron_is_valid(schedule):
                raise serializers.ValidationError(
                    {'schedule': 'Expected five cron fields, e.g. "0 9 * * 1".'}
                )
            # Valid syntax is not the same as a schedule that ever comes round;
            # `0 0 30 2 *` parses and never fires. Caught here rather than left
            # as a NULL `next_due_at` nobody can interpret.
            if next_run_after(schedule, timezone.now(), tz) is None:
                raise serializers.ValidationError(
                    {'schedule': f'"{schedule}" has no next run — check the day '
                                 f'and month fields.'}
                )

        # Keyed off the schedule, not the trigger mode: `sync_schedule` creates
        # a Trigger for any non-blank cron, and the sweep calls the runtime with
        # `caller='trigger'`, which `_check_unattended` refuses unless this is
        # on. Same reasoning as the branch above — a schedule that cannot fire
        # is a silent no-op, except this one fails five times and then disables
        # the trigger, so the user loses even the evidence.
        if schedule and not attrs.get('allowUnattended', False):
            raise serializers.ValidationError(
                {'allowUnattended': 'A scheduled agent must be cleared to run '
                                    'unattended, otherwise every firing is refused. '
                                    'Turn it on, or clear the schedule.'}
            )

        # Shell plus unrestricted network is the combination that turns a
        # sandbox escape into an exfiltration path. Refuse it outright rather
        # than leaving it to a reviewer to notice on the permissions screen.
        tools = attrs.get('tools') or {}
        if tools.get('shell') and attrs.get('egress', 'none') == 'full':
            raise serializers.ValidationError(
                {'egress': 'Shell access with unrestricted network egress is not allowed. '
                           'Narrow one of the two.'}
            )
        return attrs

    # ---------------- mapping ----------------

    @staticmethod
    def to_config(agent: SubAgent) -> dict:
        """Model columns -> the flat AgentConfig the builder speaks."""
        sandbox = agent.sandbox or {}
        guards = agent.guardrails or {}
        ctx = agent.agent_context or {}
        settings_ = agent.runtime_settings or {}
        # The cron lives on a `Trigger` row, because the schedule sweep queries
        # `next_due_at` across every user. Only the *builder's* row round-trips
        # through this flat field: an agent may now carry several schedules, and
        # reading back whichever happened to sort first would let a save from
        # this screen overwrite one added on the Schedules page.
        triggers_ = [t for t in agent.triggers.all() if t.mode == 'schedule']
        schedule = next((t for t in triggers_ if t.origin == 'builder'), None)
        workflow = agent  # local alias; the remaining lines read column-wise
        return {
            'id': agent.id,
            'name': agent.name,
            'brief': agent.prompt or '',
            'provider': agent.llm_provider,
            'model': agent.llm_model,
            'temperature': settings_.get('temperature', 0.2),
            'fileAccess': sandbox.get('fileAccess', 'scoped'),
            'workdir': sandbox.get('workdir', '/workspace'),
            'venv': sandbox.get('venv', True),
            'tools': {k: bool((workflow.tool_grants or {}).get(k, False)) for k in sorted(TOOL_KEYS)},
            'connectors': ctx.get('connectors', []),
            'knowledgeBases': ctx.get('knowledgeBases', []),
            'skills': ctx.get('skills', []),
            'useOrgContext': ctx.get('useOrgContext', True),
            'useEnvironment': ctx.get('useEnvironment', False),
            'trigger': settings_.get('invocationMode', 'goal'),
            'schedule': schedule.cron if schedule else '',
            'scheduleTimezone': schedule.tz if schedule else 'UTC',
            # How many *other* schedules this agent has, so the builder can say
            # so and link out rather than pretending its one field is the whole
            # picture.
            'extraSchedules': max(len(triggers_) - (1 if schedule else 0), 0),
            'allowUnattended': agent.allow_unattended,
            'autonomy': guards.get('autonomy', 'ask'),
            'notifyOnHitl': guards.get('notifyOnHitl', True),
            'reviewAgent': guards.get('reviewAgent', False),
            'spendCapRupees': guards.get('spendCapRupees', 500),
            # Read through `budget` rather than straight off the dict: an agent
            # saved before this field existed has no key at all, and every one
            # of them still has to run.
            'maxRunSeconds': budget.limit_for(agent),
            'egress': guards.get('egress', 'none'),
            'summaryModel': settings_.get('summaryModel', ''),
            'summaryProvider': settings_.get('summaryProvider', ''),
            'recursiveContext': settings_.get('recursiveContext', True),
            'compaction': settings_.get('compaction', True),
            'indexing': settings_.get('indexing', True),
            'status': workflow.status,
            'created_at': workflow.created_at,
            'updated_at': workflow.updated_at,
        }

    @staticmethod
    def apply(workflow: SubAgent, data: dict) -> SubAgent:
        """Validated AgentConfig -> model columns. Caller saves.

        The schedule is *not* applied here: it lives on a `Trigger` row, which
        needs the agent to have a primary key first. `sync_schedule` does it
        after the save.
        """
        workflow.name = data['name']
        workflow.prompt = data.get('brief', '')
        workflow.llm_provider = data.get('provider', 'openrouter')
        workflow.llm_model = data.get('model', '')

        workflow.sandbox = {
            'fileAccess': data.get('fileAccess', 'scoped'),
            'workdir': data.get('workdir', '/workspace'),
            'venv': data.get('venv', True),
        }
        # Store the full closed set, not just what was sent: an absent key must
        # read as "denied", never as "unset and therefore whatever the runtime
        # defaults to".
        sent_tools = data.get('tools') or {}
        workflow.tool_grants = {k: bool(sent_tools.get(k, False)) for k in sorted(TOOL_KEYS)}

        workflow.agent_context = {
            'connectors': data.get('connectors', []),
            'knowledgeBases': data.get('knowledgeBases', []),
            'skills': data.get('skills', []),
            'useOrgContext': data.get('useOrgContext', True),
            'useEnvironment': data.get('useEnvironment', False),
        }
        autonomy = data.get('autonomy', 'ask')
        workflow.guardrails = {
            'autonomy': autonomy,
            'notifyOnHitl': data.get('notifyOnHitl', True),
            'reviewAgent': data.get('reviewAgent', False),
            'spendCapRupees': data.get('spendCapRupees', 500),
            'maxRunSeconds': budget.clamp_run_seconds(
                data.get('maxRunSeconds', DEFAULT_RUN_SECONDS)
            ),
            'egress': data.get('egress', 'none'),
        }
        if 'allowUnattended' in data:
            workflow.allow_unattended = bool(data['allowUnattended'])

        settings_ = dict(workflow.runtime_settings or {})
        settings_.update({
            'temperature': data.get('temperature', 0.2),
            'summaryModel': data.get('summaryModel', ''),
            'summaryProvider': data.get('summaryProvider', ''),
            'recursiveContext': data.get('recursiveContext', True),
            'compaction': data.get('compaction', True),
            'indexing': data.get('indexing', True),
            'invocationMode': data.get('trigger', 'goal'),
        })
        workflow.runtime_settings = settings_
        return workflow

    @staticmethod
    def sync_schedule(agent: SubAgent, data: dict) -> None:
        """Reconcile the *builder's* schedule Trigger with the submitted config.

        Called after save, because a Trigger needs the agent's primary key.
        A blank schedule removes the row rather than leaving a disabled one:
        a trigger that exists but never fires is the kind of state that shows
        up in a listing and makes someone wonder which of the two is true.

        Scoped to `origin='builder'`. The builder carries one cron field and
        reconciles it on every PATCH — and the builder PATCHes constantly — so
        an unscoped reconcile means every save from this screen deletes or
        overwrites a schedule the user added on the Schedules page. One field
        cannot be the source of truth for a list; it can only own its own row.

        Rows predating the column need no special case here: migration 0020
        marks every one of them `builder`, which is what they were — until it
        landed, `sync_schedule` was the only thing that created a schedule.
        Guessing at read time instead ("adopt it if it is the only one") would
        make an agent's single deliberately-manual schedule indistinguishable
        from a legacy one, and quietly overwrite it.
        """
        from agents.models import Trigger

        cron = (data.get('schedule') or '').strip()
        tz = (data.get('scheduleTimezone') or 'UTC').strip() or 'UTC'
        existing = agent.triggers.filter(mode='schedule', origin='builder').first()

        if not cron:
            if existing:
                existing.delete()
            return

        # Armed from the row's own start date where it has one, matching
        # `views/triggers._arm`, so the builder and the Schedules page cannot
        # arm the same schedule to two different instants.
        now = timezone.now()

        if existing:
            after = max(now, existing.starts_at) if existing.starts_at else now
            existing.config = {'cron': cron}
            existing.timezone = tz
            existing.origin = 'builder'
            existing.goal = agent.prompt or ''
            existing.next_due_at = next_run_after(cron, after, tz)
            existing.save(update_fields=[
                'config', 'timezone', 'origin', 'goal', 'next_due_at',
                'updated_at',
            ])
            return

        Trigger.objects.create(
            subagent=agent, mode='schedule', config={'cron': cron},
            timezone=tz, origin='builder', goal=agent.prompt or '',
            next_due_at=next_run_after(cron, now, tz),
        )


def _with_stats(configs, workflows, user):
    """Attach observed behaviour to serialized agents.

    These are the numbers that say whether delegating is paying off, so they are
    counted from ExecutionLog rather than stored on the agent — a stored counter
    drifts, a query cannot.
    """
    ids = [w.id for w in workflows]
    if not ids:
        return configs

    # Two queries, not one, and both counts are `distinct`. Filtering on
    # `hitl_requests` forces a LEFT JOIN, so a run with three HITL requests
    # contributes three rows — a plain Count reported it as three runs, and a
    # plain Sum multiplied its spend by three. `Sum` has no honest de-duplicated
    # form over that join, so the spend is aggregated separately, without it.
    rows = (
        ExecutionLog.objects
        .filter(user=user, subagent_id__in=ids)
        .values('subagent_id')
        .annotate(
            runs=Count('id', distinct=True),
            # A run nobody had to touch: no HITL request was raised against it.
            unattended=Count('id', filter=Q(hitl_requests__isnull=True),
                             distinct=True),
        )
    )
    # Spend is derived from `tokens_used` through the same conversion the spend
    # cap refuses runs on — see agents/spend.py. It used to sum `credits_used`,
    # which nothing writes, so every agent reported a spend of zero.
    spend_rows = (
        ExecutionLog.objects
        .filter(user=user, subagent_id__in=ids)
        .values('subagent_id')
        .annotate(tokens=Sum('tokens_used'))
    )
    spend_by_id = {r['subagent_id']: rupees_for(r['tokens']) for r in spend_rows}

    by_id = {r['subagent_id']: r for r in rows}
    for cfg in configs:
        r = by_id.get(cfg['id'], {})
        cfg['runs'] = r.get('runs', 0)
        cfg['unattended'] = r.get('unattended', 0)
        cfg['spend'] = spend_by_id.get(cfg['id'], 0)
    return configs

@extend_schema(
    methods=['GET'],
    responses={200: AgentSerializer(many=True)},
    description='List the caller\'s agents, with run statistics.',
)
@extend_schema(
    methods=['POST'],
    request=AgentSerializer,
    responses={201: AgentSerializer},
    description='Create an agent from a knob-board configuration.',
)
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def agent_list(request):
    if request.method == 'GET':
        qs = (SubAgent.objects.filter(user=request.user)
              .prefetch_related('triggers').order_by('-updated_at'))
        workflows = list(qs)
        configs = [AgentSerializer.to_config(w) for w in workflows]
        return Response(_with_stats(configs, workflows, request.user))

    serializer = AgentSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)

    # `unique_together = (user, name)` backs the de-duplication below, so a
    # duplicate create gets a suffixed name rather than a 500.
    base_name = serializer.validated_data['name']
    name = base_name
    counter = 1
    while SubAgent.objects.filter(user=request.user, name=name).exists():
        name = f'{base_name} ({counter})'
        counter += 1

    data = dict(serializer.validated_data, name=name)
    agent = AgentSerializer.apply(SubAgent(user=request.user), data)
    agent.save()
    AgentSerializer.sync_schedule(agent, data)
    # Revision 1. Every later save diffs against this, so an agent with no
    # creation revision would show its first edit as though it invented the
    # whole configuration.
    revisions.record(agent, user=request.user, source='create')
    logger.info('Agent %s created by user %s', agent.id, request.user.id)
    return Response(_with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0],
                    status=status.HTTP_201_CREATED)


@extend_schema(methods=['GET'], responses={200: AgentSerializer})
@extend_schema(methods=['PUT', 'PATCH'], request=AgentSerializer, responses={200: AgentSerializer})
@extend_schema(methods=['DELETE'], responses={204: OpenApiResponse(description='Agent deleted')})
@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def agent_detail(request, agent_id: int):
    agent = get_object_or_404(SubAgent, id=agent_id, user=request.user)

    if request.method == 'GET':
        return Response(_with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0])

    if request.method == 'DELETE':
        agent.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    # PATCH merges onto the current config so a partial save cannot silently
    # reset an unsent knob to its default — which, for a grant, would mean
    # quietly widening or narrowing what the agent may do.
    incoming = request.data
    if request.method == 'PATCH':
        current = AgentSerializer.to_config(agent)
        merged = {k: v for k, v in current.items()
                  if k not in ('id', 'status', 'created_at', 'updated_at')}
        merged.update(incoming)
        incoming = merged

    serializer = AgentSerializer(data=incoming, context={'request': request})
    serializer.is_valid(raise_exception=True)

    name = serializer.validated_data['name']
    if SubAgent.objects.filter(user=request.user, name=name).exclude(id=agent.id).exists():
        return Response({'error': 'You already have an agent with that name.'},
                        status=status.HTTP_400_BAD_REQUEST)

    AgentSerializer.apply(agent, serializer.validated_data).save()
    # Schedule first, *then* the revision. `to_config` reads the cron off the
    # agent's Trigger row, so recording before the sync snapshots the previous
    # schedule — every revision would show the edit before this one. `agent_list`
    # has always done it in this order; this path did not.
    AgentSerializer.sync_schedule(agent, serializer.validated_data)
    revisions.record(agent, user=request.user, source='update')
    return Response(_with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0])
