"""
The template gallery API: browse `agents/gallery.py`, then install one.

Three endpoints, and the middle one is the whole point.

`GET  templates/`                — the catalogue, with the caller's own
                                   candidates attached to every requirement.
`GET  templates/<slug>/`         — one entry, same shape.
`POST templates/<slug>/install/` — create a `SubAgent` from it.

**Install goes through `AgentSerializer`, not around it.** A template is a
flat `AgentConfig`, so installing is the same act as saving in the builder:
same validation, same closed set of tool grants, same ownership checks on every
id. A second write path would be a second place for a guardrail check to be
forgotten — the mistake `agents/agent/runtime.py`'s "one door" note exists to
name — and here it would be the check that stops an installed agent naming
somebody else's knowledge base.

**Requirements are resolved against the caller, twice.** `_resolve` maps
requirement keys to the ids the installer chose, and the serializer then
re-validates every one of them against `request.user`. The first pass is about
shape (was a required requirement answered?), the second about ownership, and
neither substitutes for the other.

**Candidates are computed, not guessed.** The connection pool comes from
`visible_servers_sync`, which is the same predicate the serializer validates
against; the knowledge base and skill pools are the caller's own rows. A picker
offering something the validator would refuse is worse than an empty picker.
"""
from __future__ import annotations

import logging

from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from inference.models import KnowledgeBase
from logs import revisions
from skills.models import Skill

from agents import gallery
from agents.models import SubAgent
from agents.triggers import zone_is_valid
from agents.views.agents import AgentSerializer, _with_stats

logger = logging.getLogger(__name__)


def _candidates(user) -> dict[str, list[dict]]:
    """What this user could satisfy each requirement kind with.

    Three queries, done once per response rather than per requirement: the
    catalogue is small and several entries ask for the same kind of thing.
    """
    from mcp_integration.client import visible_servers_sync

    return {
        'connector': [
            {'id': s.id, 'label': s.label, 'icon_slug': s.icon_slug,
             'category': s.category}
            for s in visible_servers_sync(user.id)
        ],
        'knowledge_base': [
            {'id': kb.id, 'label': kb.name, 'doc_count': kb.doc_count,
             'backend': kb.backend}
            for kb in KnowledgeBase.objects.filter(user=user).order_by('name')
        ],
        'skill': [
            {'id': s.id, 'label': s.title}
            for s in Skill.objects.filter(user=user).order_by('title')
        ],
    }


def _present(entry: dict, candidates: dict[str, list[dict]]) -> dict:
    """One catalogue entry as the gallery renders it.

    The permissions screen is built from `config` — the same keys the
    serializer stores and the runtime enforces — so this deliberately hands
    the config over whole rather than summarising it into a second vocabulary
    that could drift from the first.
    """
    requirements = []
    for req in entry.get('requirements') or []:
        pool = candidates.get(req['type'], [])
        provider = req.get('provider')
        # A provider hint narrows the pool but never empties it: a user whose
        # Gmail connection is named something else must still be able to pick
        # it, so the hint reorders rather than filters.
        if provider:
            pool = (
                [c for c in pool if c.get('icon_slug') == provider]
                + [c for c in pool if c.get('icon_slug') != provider]
            )
        requirements.append({**req, 'optional': bool(req.get('optional')),
                             'candidates': pool})

    return {
        'slug': entry['slug'],
        'name': entry['name'],
        'tagline': entry['tagline'],
        'description': entry['description'],
        'icon': entry.get('icon', ''),
        'tags': entry.get('tags', []),
        'requirements': requirements,
        'config': entry['config'],
    }


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description='The template catalogue.')},
    description='Agent templates available to install, with the caller\'s own '
                'connections, knowledge bases and skills attached to each '
                'requirement as candidates.',
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def template_list(request):
    candidates = _candidates(request.user)
    return Response([_present(e, candidates) for e in gallery.listing()])


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description='One template.'),
               404: OpenApiResponse(description='No such template.')},
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def template_detail(request, slug: str):
    entry = gallery.get(slug)
    if entry is None:
        return Response({'error': f'No template named "{slug}".'},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(_present(entry, _candidates(request.user)))


def _resolve(entry: dict, chosen: dict) -> tuple[dict, list[str]]:
    """Requirement key -> id becomes the `AgentConfig` id lists.

    Returns `(fields, errors)`. Nothing here checks *ownership* — the
    serializer does that against `request.user`, and doing it in one place is
    what keeps the install path and the builder path enforcing the same rule.
    """
    fields: dict[str, list[int]] = {}
    errors: list[str] = []

    for req in entry.get('requirements') or []:
        key = req['key']
        value = chosen.get(key)
        if value in (None, '', []):
            if not req.get('optional'):
                errors.append(f'"{req["label"]}" is required.')
            continue
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            errors.append(f'"{req["label"]}" must be an id.')
            continue
        field = gallery.REQUIREMENT_FIELDS[req['type']]
        fields.setdefault(field, []).append(resolved)

    return fields, errors


@extend_schema(
    methods=['POST'],
    responses={201: AgentSerializer,
               400: OpenApiResponse(description='Unsatisfied requirement, or a '
                                                'configuration the serializer '
                                                'refused.'),
               404: OpenApiResponse(description='No such template.')},
    description='Install a template as one of the caller\'s own agents. Body: '
                '{"name": optional override, "requirements": {key: id}, '
                '"timezone": IANA zone for any schedule the template carries}.',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def template_install(request, slug: str):
    entry = gallery.get(slug)
    if entry is None:
        return Response({'error': f'No template named "{slug}".'},
                        status=status.HTTP_404_NOT_FOUND)

    chosen = request.data.get('requirements') or {}
    if not isinstance(chosen, dict):
        return Response({'error': 'requirements must be an object of '
                                  '{requirement key: id}.'},
                        status=status.HTTP_400_BAD_REQUEST)

    fields, errors = _resolve(entry, chosen)
    if errors:
        return Response({'error': ' '.join(errors), 'requirements': errors},
                        status=status.HTTP_400_BAD_REQUEST)

    config = {**entry['config'], **fields}

    requested_name = (request.data.get('name') or '').strip()
    if requested_name:
        config['name'] = requested_name

    # Only meaningful when the template ships a cron. A zone on an agent with
    # no schedule is a stored value nothing reads, and an invalid one would
    # then 400 an install for a field the installer never saw.
    if config.get('schedule'):
        tz = (request.data.get('timezone') or '').strip()
        if tz and not zone_is_valid(tz):
            return Response(
                {'error': f'"{tz}" is not an IANA timezone name, e.g. '
                          f'"Asia/Kolkata".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if tz:
            config['scheduleTimezone'] = tz

    serializer = AgentSerializer(data=config, context={'request': request})
    serializer.is_valid(raise_exception=True)

    # Same de-duplication as `agent_list`, and for the same reason: installing
    # a template twice is an ordinary thing to do, and `unique_together
    # (user, name)` would otherwise turn the second one into a 500.
    base_name = serializer.validated_data['name']
    name = base_name
    counter = 1
    while SubAgent.objects.filter(user=request.user, name=name).exists():
        name = f'{base_name} ({counter})'
        counter += 1

    data = dict(serializer.validated_data, name=name)

    with transaction.atomic():
        agent = AgentSerializer.apply(SubAgent(user=request.user), data)
        # The tags say where it came from, so an installed agent is
        # distinguishable from one built by hand a month later.
        agent.tags = list(entry.get('tags') or []) + [f'template:{slug}']
        agent.icon = entry.get('icon', '')
        agent.save()
        AgentSerializer.sync_schedule(agent, data)
        # `source='create'`, because that is what an install is. A fourth
        # choice on `SubAgentRevision.SOURCE_CHOICES` would need a migration
        # to record a distinction nothing reads — the tag above already says
        # which template it was.
        revisions.record(agent, user=request.user, source='create')

    logger.info('Agent %s installed from template %s by user %s',
                agent.id, slug, request.user.id)
    return Response(
        _with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0],
        status=status.HTTP_201_CREATED,
    )
