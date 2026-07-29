"""
Agents API.

An agent is a `Workflow` with `kind='agent'` and no nodes — see
docs/AGENT_TEMPLATES.md §3 for why it reuses the workflow table rather than
getting a model of its own.

This module owns the translation between the frontend's `AgentConfig` (camelCase,
flat) and the model's columns (snake_case, grouped by who reads them). That
translation lives in exactly one place on purpose: the permissions screen an
installer sees is rendered from `tool_grants` / `guardrails` / `requirements`,
and the runtime enforces from those same columns. If a second mapping existed
the screen could promise something the runtime never checks.

Everything here is scoped to `request.user`. Ids that point at other rows
(knowledge bases, skills) are re-checked against the caller — an agent that
could attach someone else's knowledge base would be a cross-tenant read.
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
from logs.models import ExecutionLog
from skills.models import Skill

from .models import Workflow
from .serializers import SUSPICIOUS_WORKFLOW_NAME_RE

logger = logging.getLogger(__name__)


# The closed sets. Anything outside them is rejected rather than stored, because
# an unknown tool key would sail through the permissions screen (which renders
# what it knows) and land in the runtime unrecognised.
TOOL_KEYS = {'codeExecution', 'shell', 'webSearch', 'scrape', 'fileOps', 'rag'}
CONNECTOR_IDS = {'gdrive', 'gmail', 'sheets', 'photos', 'calendar', 'slack'}
FILE_ACCESS = {'none', 'readonly', 'scoped', 'full'}
TRIGGER_MODES = {'goal', 'maintenance', 'template'}
AUTONOMY = {'full', 'ask', 'review'}
EGRESS = {'none', 'allowlist', 'full'}

# Ceilings on what a single agent may reserve. Not a billing control — a blast
# radius one, so a typo in a spinner cannot ask for 512 GB.
MAX_CPU = 8
MAX_MEMORY_MB = 8192
MIN_MEMORY_MB = 256

# Supervision is the existing workflow-level concept; autonomy is the agent-level
# word for the same thing. Map rather than duplicate, so the HITL path that
# already reads supervision_level keeps working for agents unchanged.
AUTONOMY_TO_SUPERVISION = {'full': 'none', 'ask': 'error_only', 'review': 'full'}
SUPERVISION_TO_AUTONOMY = {v: k for k, v in AUTONOMY_TO_SUPERVISION.items()}


def _cron_looks_valid(expr: str) -> bool:
    """Five whitespace-separated fields. Deliberately shallow — the scheduler is
    the real validator; this only catches obvious typos before they are saved."""
    fields = expr.split()
    if len(fields) != 5:
        return False
    allowed = set('0123456789*/,-')
    return all(f and set(f) <= allowed for f in fields)


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
    cpu = serializers.IntegerField(required=False, default=1, min_value=1, max_value=MAX_CPU)
    memoryMb = serializers.IntegerField(
        required=False, default=1024, min_value=MIN_MEMORY_MB, max_value=MAX_MEMORY_MB
    )

    # Tools
    tools = serializers.DictField(child=serializers.BooleanField(), required=False)

    # Context it is given
    connectors = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    knowledgeBases = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    skills = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    useOrgContext = serializers.BooleanField(required=False, default=True)
    useEnvironment = serializers.BooleanField(required=False, default=False)

    # Invocation
    trigger = serializers.ChoiceField(choices=sorted(TRIGGER_MODES), required=False, default='goal')
    schedule = serializers.CharField(required=False, allow_blank=True, default='')

    # Guardrails
    autonomy = serializers.ChoiceField(choices=sorted(AUTONOMY), required=False, default='ask')
    notifyOnHitl = serializers.BooleanField(required=False, default=True)
    reviewAgent = serializers.BooleanField(required=False, default=False)
    spendCapRupees = serializers.IntegerField(required=False, default=500, min_value=0)
    # The knob the design note flagged as missing: an agent that can run code but
    # cannot reach the network is a very different thing to grant a stranger.
    egress = serializers.ChoiceField(choices=sorted(EGRESS), required=False, default='none')

    # Context lifecycle
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
        unknown = set(value) - CONNECTOR_IDS
        if unknown:
            raise serializers.ValidationError(f"Unknown connectors: {', '.join(sorted(unknown))}.")
        return sorted(set(value))

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
        if trigger == 'maintenance':
            if not schedule:
                raise serializers.ValidationError(
                    {'schedule': 'A maintenance agent needs a schedule, otherwise it never runs.'}
                )
            if not _cron_looks_valid(schedule):
                raise serializers.ValidationError(
                    {'schedule': 'Expected five cron fields, e.g. "0 9 * * 1".'}
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
    def to_config(workflow: Workflow) -> dict:
        """Model columns -> the flat AgentConfig the builder speaks."""
        sandbox = workflow.sandbox or {}
        guards = workflow.guardrails or {}
        ctx = workflow.agent_context or {}
        trig = workflow.trigger or {}
        settings_ = workflow.workflow_settings or {}
        return {
            'id': workflow.id,
            'name': workflow.name,
            'brief': workflow.context or '',
            'provider': workflow.llm_provider,
            'model': workflow.llm_model,
            'temperature': settings_.get('temperature', 0.2),
            'fileAccess': sandbox.get('fileAccess', 'scoped'),
            'workdir': sandbox.get('workdir', '/workspace'),
            'venv': sandbox.get('venv', True),
            'cpu': sandbox.get('cpu', 1),
            'memoryMb': sandbox.get('memoryMb', 1024),
            'tools': {k: bool((workflow.tool_grants or {}).get(k, False)) for k in sorted(TOOL_KEYS)},
            'connectors': ctx.get('connectors', []),
            'knowledgeBases': ctx.get('knowledgeBases', []),
            'skills': ctx.get('skills', []),
            'useOrgContext': ctx.get('useOrgContext', True),
            'useEnvironment': ctx.get('useEnvironment', False),
            'trigger': trig.get('mode', 'goal'),
            'schedule': trig.get('cron', ''),
            'autonomy': guards.get('autonomy', SUPERVISION_TO_AUTONOMY.get(workflow.supervision_level, 'ask')),
            'notifyOnHitl': guards.get('notifyOnHitl', True),
            'reviewAgent': guards.get('reviewAgent', False),
            'spendCapRupees': guards.get('spendCapRupees', 500),
            'egress': guards.get('egress', 'none'),
            'recursiveContext': settings_.get('recursiveContext', True),
            'compaction': settings_.get('compaction', True),
            'indexing': settings_.get('indexing', True),
            'status': workflow.status,
            'created_at': workflow.created_at,
            'updated_at': workflow.updated_at,
        }

    @staticmethod
    def apply(workflow: Workflow, data: dict) -> Workflow:
        """Validated AgentConfig -> model columns. Caller saves."""
        workflow.kind = 'agent'
        workflow.nodes = []
        workflow.edges = []
        workflow.name = data['name']
        workflow.context = data.get('brief', '')
        workflow.llm_provider = data.get('provider', 'openrouter')
        workflow.llm_model = data.get('model', '')

        workflow.sandbox = {
            'fileAccess': data.get('fileAccess', 'scoped'),
            'workdir': data.get('workdir', '/workspace'),
            'venv': data.get('venv', True),
            'cpu': data.get('cpu', 1),
            'memoryMb': data.get('memoryMb', 1024),
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
        workflow.trigger = {
            'mode': data.get('trigger', 'goal'),
            'cron': (data.get('schedule') or '').strip(),
        }
        autonomy = data.get('autonomy', 'ask')
        workflow.guardrails = {
            'autonomy': autonomy,
            'notifyOnHitl': data.get('notifyOnHitl', True),
            'reviewAgent': data.get('reviewAgent', False),
            'spendCapRupees': data.get('spendCapRupees', 500),
            'egress': data.get('egress', 'none'),
        }
        workflow.supervision_level = AUTONOMY_TO_SUPERVISION[autonomy]

        settings_ = dict(workflow.workflow_settings or {})
        settings_.update({
            'temperature': data.get('temperature', 0.2),
            'recursiveContext': data.get('recursiveContext', True),
            'compaction': data.get('compaction', True),
            'indexing': data.get('indexing', True),
        })
        workflow.workflow_settings = settings_
        return workflow


def _with_stats(configs, workflows, user):
    """Attach observed behaviour to serialized agents.

    These are the numbers that say whether delegating is paying off, so they are
    counted from ExecutionLog rather than stored on the agent — a stored counter
    drifts, a query cannot.
    """
    ids = [w.id for w in workflows]
    if not ids:
        return configs
    rows = (
        ExecutionLog.objects
        .filter(user=user, workflow_id__in=ids)
        .values('workflow_id')
        .annotate(
            runs=Count('id'),
            # A run nobody had to touch: no HITL request was raised against it.
            unattended=Count('id', filter=Q(hitl_requests__isnull=True)),
            spend=Sum('credits_used'),
        )
    )
    by_id = {r['workflow_id']: r for r in rows}
    for cfg in configs:
        r = by_id.get(cfg['id'], {})
        cfg['runs'] = r.get('runs', 0)
        cfg['unattended'] = r.get('unattended', 0)
        cfg['spend'] = r.get('spend') or 0
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
        qs = Workflow.objects.filter(user=request.user, kind='agent').order_by('-updated_at')
        workflows = list(qs)
        configs = [AgentSerializer.to_config(w) for w in workflows]
        return Response(_with_stats(configs, workflows, request.user))

    serializer = AgentSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)

    # `unique_together = (user, name)` covers workflows and agents alike, so
    # de-duplicate the way workflow creation already does rather than 500ing.
    base_name = serializer.validated_data['name']
    name = base_name
    counter = 1
    while Workflow.objects.filter(user=request.user, name=name).exists():
        name = f'{base_name} ({counter})'
        counter += 1

    data = dict(serializer.validated_data, name=name)
    agent = AgentSerializer.apply(Workflow(user=request.user), data)
    agent.save()
    logger.info('Agent %s created by user %s', agent.id, request.user.id)
    return Response(_with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0],
                    status=status.HTTP_201_CREATED)


@extend_schema(methods=['GET'], responses={200: AgentSerializer})
@extend_schema(methods=['PUT', 'PATCH'], request=AgentSerializer, responses={200: AgentSerializer})
@extend_schema(methods=['DELETE'], responses={204: OpenApiResponse(description='Agent deleted')})
@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def agent_detail(request, agent_id: int):
    agent = get_object_or_404(Workflow, id=agent_id, user=request.user, kind='agent')

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
    if Workflow.objects.filter(user=request.user, name=name).exclude(id=agent.id).exists():
        return Response({'error': 'You already have an agent or workflow with that name.'},
                        status=status.HTTP_400_BAD_REQUEST)

    AgentSerializer.apply(agent, serializer.validated_data).save()
    return Response(_with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0])
