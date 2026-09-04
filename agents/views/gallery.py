"""
Explore: the catalogue you install a starting point from, and publishing into it.

Two sources, one shape. A **curated template** is code (`agents/gallery.py`); a
**shared agent** is a `SharedAgent` row somebody published. They differ in
provenance and in nothing else the installer cares about, so they are presented
with the same keys and installed by the same function. A second install path
would be a second place to forget the ownership check — and here that check is
the entire reason installing a stranger's agent is safe.

`GET  templates/`                — everything installable, both sources.
`GET  templates/{slug}/`         — one entry.
`POST templates/{slug}/install/` — create an agent from it.
`GET  agents/{id}/share/`        — what publishing this agent *would* send.
`POST agents/{id}/share/`        — publish or republish it.
`DELETE agents/{id}/share/`      — withdraw it from the listing.

**Install goes through `AgentSerializer`, not around it.** Both sources carry a
flat `AgentConfig`, so installing is the same act as saving in the builder:
same validation, same closed set of tool grants, same ownership check on every
id the installer supplies.

**Requirements are resolved against the caller, twice.** `_resolve` maps
requirement keys to the ids the installer chose; the serializer then
re-validates every one against `request.user`. The first pass is about shape
(was a required requirement answered?), the second about ownership, and neither
substitutes for the other.

**Candidates are computed, not guessed.** The connection pool comes from
`visible_servers_sync`, the same predicate the serializer validates against. A
picker offering something the validator would refuse is worse than an empty
picker.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from django.utils.text import slugify
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from inference.models import KnowledgeBase
from logs import revisions
from skills.models import Skill

from agents import gallery, publishing
from agents.models import SharedAgent, SubAgent
from agents.triggers import zone_is_valid
from agents.views.agents import AgentSerializer, _with_stats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- candidates


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


def _with_candidates(requirements, candidates: dict[str, list[dict]]) -> list[dict]:
    """Attach the caller's own options to each requirement."""
    out = []
    for req in requirements or []:
        pool = candidates.get(req.get('type'), [])
        provider = req.get('provider')
        # A provider hint narrows the pool but never empties it: a user whose
        # Gmail connection is named something else must still be able to pick
        # it, so the hint reorders rather than filters.
        if provider:
            pool = (
                [c for c in pool if c.get('icon_slug') == provider]
                + [c for c in pool if c.get('icon_slug') != provider]
            )
        out.append({**req, 'optional': bool(req.get('optional')),
                    'candidates': pool})
    return out


# ------------------------------------------------------------------ presenting


def _present_curated(entry: dict, candidates: dict[str, list[dict]]) -> dict:
    """One catalogue entry as the gallery renders it.

    The permissions screen is built from `config` — the same keys the
    serializer stores and the runtime enforces — so this hands the config over
    whole rather than summarising it into a second vocabulary that could drift.
    """
    return {
        'slug': entry['slug'],
        'source': 'curated',
        'name': entry['name'],
        'tagline': entry['tagline'],
        'description': entry['description'],
        'icon': entry.get('icon', ''),
        'tags': entry.get('tags', []),
        'author': None,
        'install_count': None,
        'version': None,
        'requirements': _with_candidates(entry.get('requirements'), candidates),
        'config': entry['config'],
    }


def _author_name(user) -> str:
    """How a publisher is credited. Never the email address.

    `get_full_name` where they have set one, else the username. An email is an
    identifier the platform holds for contacting them, not a byline, and a
    listing visible to every user is exactly the wrong place to publish one.
    """
    return (user.get_full_name() or '').strip() or user.username


def _present_shared(share: SharedAgent, candidates: dict[str, list[dict]],
                    *, viewer=None) -> dict:
    return {
        'slug': share.slug,
        'source': 'community',
        'name': share.name,
        'tagline': share.tagline,
        'description': share.description,
        'icon': share.icon,
        'tags': share.tags or [],
        'author': _author_name(share.author),
        'is_mine': viewer is not None and share.author_id == viewer.id,
        'visibility': share.visibility,
        'is_listed': share.is_listed,
        'install_count': share.install_count,
        'version': share.version,
        'updated_at': share.updated_at,
        'requirements': _with_candidates(share.requirements, candidates),
        'config': share.config,
    }


def _listed_for(user):
    """Shared agents this user may see in a listing.

    Only `visibility='platform'` is listed. A `link` share is deliberately
    absent — it is reachable by its slug and nowhere else, which is the whole
    difference between the two. The author's own rows are included regardless
    of visibility so that publishing something unlisted does not look like it
    silently failed.
    """
    return (
        SharedAgent.objects
        .filter(Q(is_listed=True, visibility='platform') | Q(author=user))
        .select_related('author')
        .order_by('-install_count', '-updated_at')
    )


def _find_shared(slug: str, user) -> SharedAgent | None:
    """One shared agent by slug, if this caller may see it.

    A `link` share resolves here for anybody who has the slug — that *is* the
    sharing mechanism. A withdrawn one resolves only for its author, so they
    can see and relist it; for everyone else it is gone.
    """
    share = (SharedAgent.objects.select_related('author')
             .filter(slug=slug).first())
    if share is None:
        return None
    if share.is_listed or share.author_id == user.id:
        return share
    return None


# ---------------------------------------------------------------------- reads


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description='Everything installable.')},
    description='Curated templates and agents shared by other users, with the '
                'caller\'s own connections, knowledge bases and skills '
                'attached to each requirement as candidates. `?source=curated` '
                'or `?source=community` narrows it; `?mine=1` returns only the '
                'caller\'s own published agents.',
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def template_list(request):
    candidates = _candidates(request.user)
    source = request.query_params.get('source') or ''
    mine = request.query_params.get('mine') in ('1', 'true', 'True')

    entries: list[dict] = []
    if mine:
        shares = (SharedAgent.objects.filter(author=request.user)
                  .select_related('author').order_by('-updated_at'))
        return Response([_present_shared(s, candidates, viewer=request.user)
                         for s in shares])

    if source != 'community':
        entries += [_present_curated(e, candidates) for e in gallery.listing()]
    if source != 'curated':
        entries += [_present_shared(s, candidates, viewer=request.user)
                    for s in _listed_for(request.user)]
    return Response(entries)


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description='One entry.'),
               404: OpenApiResponse(description='No such template.')},
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def template_detail(request, slug: str):
    candidates = _candidates(request.user)
    entry = gallery.get(slug)
    if entry is not None:
        return Response(_present_curated(entry, candidates))

    share = _find_shared(slug, request.user)
    if share is None:
        return Response({'error': f'No template named "{slug}".'},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(_present_shared(share, candidates, viewer=request.user))


# -------------------------------------------------------------------- install


def _resolve(requirements, chosen: dict) -> tuple[dict, list[str]]:
    """Requirement key -> id becomes the `AgentConfig` id lists.

    Returns `(fields, errors)`. Nothing here checks *ownership* — the
    serializer does that against `request.user`, and doing it in one place is
    what keeps the install path and the builder path enforcing the same rule.
    """
    fields: dict[str, list[int]] = {}
    errors: list[str] = []

    for req in requirements or []:
        key = req.get('key')
        value = chosen.get(key)
        if value in (None, '', []):
            if not req.get('optional'):
                errors.append(f'"{req.get("label", key)}" is required.')
            continue
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            errors.append(f'"{req.get("label", key)}" must be an id.')
            continue
        field = gallery.REQUIREMENT_FIELDS.get(req.get('type'))
        if field is None:
            # A stored requirement of an unknown kind. Refused rather than
            # skipped: skipping installs an agent missing something it was
            # published as needing.
            errors.append(f'"{req.get("label", key)}" is of an unknown kind.')
            continue
        fields.setdefault(field, []).append(resolved)

    return fields, errors


@extend_schema(
    methods=['POST'],
    responses={201: AgentSerializer,
               400: OpenApiResponse(description='Unsatisfied requirement, or a '
                                                'configuration the serializer '
                                                'refused.'),
               404: OpenApiResponse(description='No such template.')},
    description='Install a template or a shared agent as one of the caller\'s '
                'own agents. Body: {"name": optional override, '
                '"requirements": {key: id}, "timezone": IANA zone for any '
                'schedule it carries}.',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def template_install(request, slug: str):
    share = None
    entry = gallery.get(slug)
    if entry is not None:
        base_config = entry['config']
        requirements = entry.get('requirements') or []
        tags = list(entry.get('tags') or []) + [f'template:{slug}']
        icon = entry.get('icon', '')
    else:
        share = _find_shared(slug, request.user)
        if share is None:
            return Response({'error': f'No template named "{slug}".'},
                            status=status.HTTP_404_NOT_FOUND)
        base_config = share.config
        requirements = share.requirements or []
        tags = list(share.tags or []) + [f'shared:{slug}']
        icon = share.icon

    chosen = request.data.get('requirements') or {}
    if not isinstance(chosen, dict):
        return Response({'error': 'requirements must be an object of '
                                  '{requirement key: id}.'},
                        status=status.HTTP_400_BAD_REQUEST)

    fields, errors = _resolve(requirements, chosen)
    if errors:
        return Response({'error': ' '.join(errors), 'requirements': errors},
                        status=status.HTTP_400_BAD_REQUEST)

    config = {**base_config, **fields}

    requested_name = (request.data.get('name') or '').strip()
    if requested_name:
        config['name'] = requested_name

    # Only meaningful when the entry ships a cron. A zone on an agent with no
    # schedule is a stored value nothing reads, and an invalid one would then
    # 400 an install for a field the installer never saw. The author's own zone
    # is deliberately not carried — see `publishing.SHAREABLE_KEYS`.
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
        agent.tags = tags
        agent.icon = icon
        agent.save()
        AgentSerializer.sync_schedule(agent, data)
        # `source='create'`, because that is what an install is. A fourth
        # choice on `SubAgentRevision.SOURCE_CHOICES` would need a migration to
        # record a distinction nothing reads — the tag above already says which
        # entry it came from.
        revisions.record(agent, user=request.user, source='create')
        if share is not None:
            # F() rather than read-modify-write: two people installing at once
            # would otherwise each read the same count and store the same
            # increment.
            SharedAgent.objects.filter(pk=share.pk).update(
                install_count=F('install_count') + 1
            )

    logger.info('Agent %s installed from %s by user %s',
                agent.id, slug, request.user.id)
    return Response(
        _with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0],
        status=status.HTTP_201_CREATED,
    )


# -------------------------------------------------------------------- publish


def _mint_slug(name: str, exclude_pk: int | None = None) -> str:
    """A unique public slug.

    Checked against the curated catalogue as well as the table: the two share
    one namespace because `template_detail` looks in the gallery first, so a
    shared agent that took a curated slug would be permanently unreachable.
    """
    base = slugify(name)[:180] or 'agent'
    candidate = base
    counter = 1
    while True:
        clash = SharedAgent.objects.filter(slug=candidate)
        if exclude_pk is not None:
            clash = clash.exclude(pk=exclude_pk)
        if candidate not in gallery.TEMPLATES and not clash.exists():
            return candidate
        counter += 1
        candidate = f'{base}-{counter}'


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description='What publishing would send.')},
    description='Preview the portable form of this agent: the allow-listed '
                'config that would travel, and the requirements its ids would '
                'become. Writes nothing.',
)
@extend_schema(
    methods=['POST'],
    responses={200: OpenApiResponse(description='Published (or republished).'),
               400: OpenApiResponse(description='The agent cannot be published '
                                                'as it stands.')},
    description='Publish this agent. Body: {tagline, description?, '
                'visibility?, requirements?} — `requirements` may only rewrite '
                'the generated labels, never their kinds.',
)
@extend_schema(
    methods=['DELETE'],
    responses={204: OpenApiResponse(description='Withdrawn from the listing.')},
)
@api_view(['GET', 'POST', 'DELETE'])
@permission_classes([IsAuthenticated])
def agent_share(request, agent_id: int):
    agent = get_object_or_404(SubAgent, id=agent_id, user=request.user)
    share = SharedAgent.objects.filter(subagent=agent, author=request.user).first()

    if request.method == 'DELETE':
        if share is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        # Withdrawn, not deleted: an install already made keeps working, and
        # relisting must not mint a second URL for the same thing.
        share.is_listed = False
        share.save(update_fields=['is_listed', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)

    try:
        config, generated = publishing.to_shareable(agent)
    except publishing.PublishError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if request.method == 'GET':
        return Response({
            'published': share is not None,
            'slug': share.slug if share else None,
            'visibility': share.visibility if share else 'platform',
            'is_listed': share.is_listed if share else False,
            'version': share.version if share else 0,
            'install_count': share.install_count if share else 0,
            'tagline': share.tagline if share else (agent.description or ''),
            'description': share.description if share else '',
            # The generated requirements, so the author sees exactly what will
            # travel — including the labels taken from their own row names,
            # which they can rewrite before confirming.
            'requirements': generated,
            'config': config,
        })

    tagline = (request.data.get('tagline') or '').strip()
    if not tagline:
        return Response(
            {'error': 'A one-line description is required — it is the only '
                      'thing most people will read before installing.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    visibility = request.data.get('visibility') or 'platform'
    if visibility not in dict(SharedAgent.VISIBILITY_CHOICES):
        return Response({'error': f'Unknown visibility "{visibility}".'},
                        status=status.HTTP_400_BAD_REQUEST)

    requirements = publishing.sanitise_requirements(
        generated, request.data.get('requirements')
    )

    fields = {
        'name': agent.name,
        'tagline': tagline[:200],
        'description': (request.data.get('description') or '').strip(),
        'config': config,
        'requirements': requirements,
        'tags': [t for t in (agent.tags or []) if isinstance(t, str)][:10],
        'icon': agent.icon,
        'visibility': visibility,
        'is_listed': True,
    }

    with transaction.atomic():
        if share is None:
            share = SharedAgent.objects.create(
                subagent=agent, author=request.user,
                slug=_mint_slug(agent.name), **fields,
            )
        else:
            for key, value in fields.items():
                setattr(share, key, value)
            # A republish is a new version of the same listing, so the slug is
            # kept even when the agent has been renamed — the link people have
            # must not rot because the author retitled it.
            share.version = F('version') + 1
            share.save()
            share.refresh_from_db()

    logger.info('Agent %s shared as %s (v%s) by user %s',
                agent.id, share.slug, share.version, request.user.id)
    return Response(
        _present_shared(share, _candidates(request.user), viewer=request.user)
    )
