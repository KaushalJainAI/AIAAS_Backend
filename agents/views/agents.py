"""
Agents API: list, create, read, update, delete and restore a revision.

An agent is a `SubAgent` row. Validation and the translation to and from the
frontend's `AgentConfig` are `agents/config.py`'s job (`AgentSerializer`);
these views add ownership, name de-duplication, run statistics and the
revision record around it.

Everything here is scoped to `request.user`. What is *not* here: starting or
intervening in a run (`views/runs.py`). That is a run's lifecycle; this is an
agent's configuration, and the two change for different reasons.
"""
import logging

from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from logs import revisions
from logs.models import ExecutionLog

from agents.models import SubAgent
from agents.spend import PRICED_SOURCES, rupees_for, rupees_for_usd

from agents.config import AgentSerializer

logger = logging.getLogger(__name__)


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
        .exclude(caller='eval')
        .values('subagent_id')
        .annotate(
            runs=Count('id', distinct=True),
            # A run nobody had to touch: no HITL request was raised against it.
            unattended=Count('id', filter=Q(hitl_requests__isnull=True),
                             distinct=True),
        )
    )
    # Spend goes through the same conversion the spend cap refuses runs on —
    # see agents/spend.py — so the number shown here and the number enforced
    # there cannot drift. Priced runs are summed from what they recorded;
    # unpriced ones fall back to the blended token rate rather than to zero.
    spend_rows = (
        ExecutionLog.objects
        .filter(user=user, subagent_id__in=ids)
        .exclude(caller='eval')
        .values('subagent_id')
        .annotate(
            priced_usd=Sum('cost_usd', filter=Q(cost_source__in=PRICED_SOURCES)),
            unpriced_tokens=Sum(
                'tokens_used', filter=~Q(cost_source__in=PRICED_SOURCES)
            ),
        )
    )
    spend_by_id = {
        r['subagent_id']: rupees_for_usd(r['priced_usd']) + rupees_for(r['unpriced_tokens'])
        for r in spend_rows
    }

    by_id = {r['subagent_id']: r for r in rows}
    by_workflow = {w.id: w for w in workflows}
    status_of = _model_statuses(configs)
    for cfg in configs:
        r = by_id.get(cfg['id'], {})
        cfg['runs'] = r.get('runs', 0)
        cfg['unattended'] = r.get('unattended', 0)
        cfg['spend'] = spend_by_id.get(cfg['id'], 0)
        cfg['model_status'] = status_of.get(cfg.get('model') or '', 'ok')
        # Observed, not configured — see the field note on `template_slug`.
        workflow = by_workflow.get(cfg['id'])
        cfg['template_slug'] = workflow.template_slug if workflow else None
    return configs


def _model_statuses(configs) -> dict[str, str]:
    """`retired` for each configured model whose catalogue row is inactive.

    Attached here rather than in `to_config`: that dict is also the revision
    snapshot, and a model being retired upstream is not a configuration change
    the owner made — it must not mint a revision. One query for the whole list.
    """
    from llm.models import AIModel

    values = {c.get('model') for c in configs if c.get('model')}
    if not values:
        return {}
    inactive = AIModel.objects.filter(value__in=values, is_active=False)
    return {value: 'retired' for value in inactive.values_list('value', flat=True)}

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


#: Keys a revision snapshot carries that are observations or identity, not
#: configuration — restoring them would be restoring the past's statistics.
#: `status` is left out too: rolling a configuration back is not a request to
#: pause or unpause the agent.
_NOT_RESTORED = frozenset({
    'id', 'status', 'runs', 'unattended', 'spend', 'created_at', 'updated_at',
    'extraSchedules', 'trigger',
})


@extend_schema(
    methods=['POST'],
    request=None,
    responses={200: AgentSerializer},
    description="Put an agent's configuration back to an earlier revision.",
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def agent_restore_revision(request, agent_id: int, number: int):
    """Roll back to revision `number`, as a new revision.

    A revision is a full `to_config` snapshot, so this is an ordinary save of
    that snapshot — through the same serializer, ownership checks and schedule
    reconcile as every other save. That matters more than it looks: a
    connection or knowledge base the old config named may since have been
    deleted or switched off, and the answer then is the serializer's own 400
    naming it, not a restore that silently re-grants access to something the
    user can no longer see. History is never rewritten: the restore is
    recorded as the next revision, with source `restore`.
    """
    from logs.models import SubAgentRevision

    agent = get_object_or_404(SubAgent, id=agent_id, user=request.user)
    revision = get_object_or_404(SubAgentRevision, subagent=agent, number=number)

    current = AgentSerializer.to_config(agent)
    restored = {k: v for k, v in (revision.config or {}).items()
                if k not in _NOT_RESTORED}
    merged = {k: v for k, v in current.items() if k not in _NOT_RESTORED}
    merged.update(restored)

    serializer = AgentSerializer(data=merged, context={'request': request})
    serializer.is_valid(raise_exception=True)
    name = serializer.validated_data['name']
    if SubAgent.objects.filter(user=request.user, name=name).exclude(id=agent.id).exists():
        return Response(
            {'error': f'Another agent is now called "{name}"; rename one first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    AgentSerializer.apply(agent, serializer.validated_data).save()
    AgentSerializer.sync_schedule(agent, serializer.validated_data)
    revisions.record(agent, user=request.user, source='restore')
    return Response(_with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0])
