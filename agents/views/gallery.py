"""
Explore: the catalogue you install a starting point from, and publishing into it.

Two sources, one shape. A **curated template** is code (`agents/gallery/`); a
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
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from inference.models import visible_knowledge_bases
from logs import revisions
from skills.models import Skill

from agents import gallery, publishing
from agents.models import SharedAgent, SubAgent
from agents.triggers import zone_is_valid
from agents.views.capabilities import unavailable_grants
from workflow_backend.thresholds import PUBLIC_CATALOGUE_LIMIT
from agents.config import AgentSerializer
from agents.views.agents import _with_stats

logger = logging.getLogger(__name__)


# --------------------------------------------------------- engine availability
#
# A template holding a grant whose engine is `none` installs an agent that can
# only talk: the runtime silently withholds `compute`/`shell`/`browser`/... —
# so Explore says so *before* the install, and the install refuses (409)
# rather than writing a row that cannot run. One predicate
# (`capabilities.unavailable_grants`), read here and rendered there.


def _availability(config: dict | None) -> tuple[bool, str]:
    """Whether `config` can run on this server, and why not."""
    blocked = unavailable_grants(config)
    if not blocked:
        return True, ''
    grants = ', '.join(sorted({b['grant'] for b in blocked}))
    return False, (
        f'Requires {grants}, which is not available on this server: '
        f"{blocked[0]['reason']}"
    )


# ---------------------------------------------------------------- candidates


def _candidates(user) -> dict[str, list[dict]]:
    """What this user could satisfy each requirement kind with.

    Done once per response rather than per requirement: the catalogue is
    small and several entries ask for the same kind of thing. Custom-tool
    pools are the caller's own connections — picking one reuses it, while
    `'install'` (handled in `_resolve`) takes the author's frozen copy.
    """
    from datasources.models import ApiConnection, DataConnection
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
            for kb in visible_knowledge_bases(user).order_by('name')
        ],
        'skill': [
            {'id': s.id, 'label': s.title}
            for s in Skill.objects.filter(user=user).order_by('title')
        ],
        'api_tool': [
            {'id': r.id, 'label': r.name, 'base_url': r.base_url}
            for r in ApiConnection.objects.filter(user=user).order_by('name')
        ],
        'data_tool': [
            {'id': r.id, 'label': r.name, 'kind': r.kind}
            for r in DataConnection.objects.filter(user=user).order_by('name')
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
    `available` says whether the entry can run on this server at all (a grant
    whose engine is `none`), so Explore can badge it instead of installing an
    agent that can only talk.
    """
    available, unavailable_reason = _availability(entry.get('config'))
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
        'available': available,
        **({'unavailable_reason': unavailable_reason} if unavailable_reason else {}),
        # Which one-click pack this installs with, if any — computed from
        # `PACKS`, never stored, so the catalogue cannot disagree with the
        # pack. What the Explore page groups by. Shared entries carry None:
        # they belong to no pack.
        'pack': gallery.pack_of(entry['slug']),
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
    available, unavailable_reason = _availability(share.config or {})
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
        'available': available,
        **({'unavailable_reason': unavailable_reason} if unavailable_reason else {}),
        # No pack: only curated entries belong to one.
        'pack': None,
        'requirements': _with_candidates(share.requirements, candidates),
        'config': share.config,
    }


def _listed_for(user):
    """Shared agents this user may see in a listing.

    `link` shares are deliberately absent — reachable by slug and nowhere
    else, which is the whole difference between them and the wider rungs. The
    author's own rows are included regardless of visibility so that publishing
    something unlisted does not look like it silently failed.
    """
    return (
        SharedAgent.objects
        .filter(Q(is_listed=True,
                  visibility__in=SharedAgent.LISTED_VISIBILITIES)
                | Q(author=user))
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


def _resolve(requirements, chosen: dict, user) -> tuple[dict, list, list]:
    """Requirement key -> id becomes the `AgentConfig` id lists.

    Returns `(fields, errors, installed)`. Nothing here checks *ownership* of
    chosen ids — the serializer does that against `request.user`, and doing
    it in one place is what keeps the install path and the builder path
    enforcing the same rule.

    Custom tools (`api_tool` / `data_tool`) accept `'install'` instead of an
    id: the author's frozen snapshot is installed as the caller's own private
    copy. `installed` reports each copy plus the credential type to link, so
    the installer knows what is still unauthenticated.
    """
    from datasources import sharing as _tool_sharing

    fields: dict[str, list] = {}
    errors: list[str] = []
    installed: list[dict] = []

    for req in requirements or []:
        key = req.get('key')
        kind = req.get('type')
        value = chosen.get(key)
        if value in (None, '', []):
            if not req.get('optional'):
                errors.append(f'"{req.get("label", key)}" is required.')
            continue
        field = gallery.REQUIREMENT_FIELDS.get(kind)
        if field is None:
            # A stored requirement of an unknown kind. Refused rather than
            # skipped: skipping installs an agent missing something it was
            # published as needing.
            errors.append(f'"{req.get("label", key)}" is of an unknown kind.')
            continue
        if kind in ('api_tool', 'data_tool') and value == 'install':
            snapshot = req.get('snapshot') or {}
            try:
                row, needs = _tool_sharing.install_copy(
                    snapshot.get('tool_kind'), snapshot.get('config') or {},
                    snapshot.get('auth_shape') or {}, user)
            except Exception as exc:  # noqa: BLE001 — serializer detail
                detail = getattr(exc, 'detail', str(exc))
                errors.append(f'"{req.get("label", key)}" could not be '
                              f'installed: {detail}')
                continue
            entry: object = row.id
            if kind == 'api_tool' and req.get('mode') == 'read':
                entry = {'id': row.id, 'mode': 'read'}
            fields.setdefault(field, []).append(entry)
            installed.append({
                'tool': row.name,
                'tool_kind': snapshot.get('tool_kind'),
                'connection_id': row.id,
                **({'needs': needs} if needs else {}),
            })
            continue
        try:
            resolved = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            errors.append(f'"{req.get("label", key)}" must be an id.')
            continue
        fields.setdefault(field, []).append(resolved)

    return fields, errors, installed


@extend_schema(
    methods=['POST'],
    responses={201: AgentSerializer,
               400: OpenApiResponse(description='Unsatisfied requirement, or a '
                                                'configuration the serializer '
                                                'refused.'),
               404: OpenApiResponse(description='No such template.'),
               409: OpenApiResponse(description='The entry needs an engine '
                                                'this server has not '
                                                'configured.')},
     description='Install a template or a shared agent as one of the caller\'s '
                 'own agents. Body: {"name": optional override, '
                 '"requirements": {key: id}, "timezone": IANA zone for any '
                 'schedule it carries}. A custom-tool requirement also '
                 'accepts "install" to take the author\'s frozen copy as a '
                 'private, unauthenticated tool of your own. 409 when the entry '
                 'holds a grant whose engine is `none` on this server — '
                 'installing it would write an agent that can only talk.',
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

    available, unavailable_reason = _availability(base_config)
    if not available:
        # 409, not 400: the request is well-formed, the server cannot honour
        # it — installing would write an agent that can only talk.
        return Response(
            {'error': f'"{slug}" cannot run on this server. {unavailable_reason}',
             'unavailable_reason': unavailable_reason},
            status=status.HTTP_409_CONFLICT,
        )

    fields, errors, installed = _resolve(requirements, chosen, request.user)
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
        # Curated installs record their slug so a pack install can skip what
        # is already there. Shared installs leave this empty: their slug lives
        # in another user's namespace.
        agent.template_slug = slug if entry is not None else None
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
        if entry is not None and slug == 'coding-lead':
            # A lone lead installed after its roster still gets its scope.
            _wire_coding_lead(request.user)

    logger.info('Agent %s installed from %s by user %s',
                agent.id, slug, request.user.id)
    body = _with_stats([AgentSerializer.to_config(agent)], [agent],
                       request.user)[0]
    if installed:
        # Tools the install carried over: private copies owned by the
        # installer, unauthenticated until they link their own credentials.
        body['installed_tools'] = installed
        body['credentials_needed'] = [
            {'tool': item['tool'], **item['needs']}
            for item in installed if item.get('needs')
        ]
    return Response(body, status=status.HTTP_201_CREATED)


@extend_schema(
    methods=['POST'],
    responses={200: OpenApiResponse(description='Pack install result.'),
               409: OpenApiResponse(description='Nothing in the pack can run '
                                                'on this server.')},
    description='Install every template in a pack that needs no setup and is '
                'not already installed. Body: {"pack": "office"}. Templates '
                'with requirements are listed as needing setup rather than '
                'installed. Templates holding a grant whose engine is `none` '
                'on this server are skipped as engine-unavailable rather than '
                'installed unable to run — and when *every* setup-free member '
                'is engine-blocked the pack answers 409 instead of an empty '
                'install. Idempotent: reinstalling skips what is already there. '
                'Optional "overrides": {slug: AgentConfig-fragment} — per-template '
                'tightening (autonomy, writePaths, commandScope, toolPermissions) '
                'applied through the same serializer the builder saves through.',
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def template_install_pack(request):
    pack = (request.data.get('pack') or '').strip()
    slugs = gallery.PACKS.get(pack) if hasattr(gallery, 'PACKS') else None
    if not slugs:
        return Response({'error': f'No pack named "{pack}".'},
                        status=status.HTTP_404_NOT_FOUND)

    overrides = request.data.get('overrides') or {}
    if not isinstance(overrides, dict):
        return Response({'error': '"overrides" is an object of {template slug: config}.'},
                        status=status.HTTP_400_BAD_REQUEST)

    installed: list[dict] = []
    skipped: list[dict] = []
    engine_blocked: list[dict] = []
    for slug in slugs:
        entry = gallery.get(slug)
        if entry is None:
            skipped.append({'slug': slug, 'reason': 'no such template'})
            continue
        if SubAgent.objects.filter(user=request.user, template_slug=slug).exists():
            skipped.append({'slug': slug, 'reason': 'already installed'})
            continue
        requirements = entry.get('requirements') or []
        if [r for r in requirements if not r.get('optional')]:
            skipped.append({'slug': slug, 'reason': 'needs setup'})
            continue
        available, unavailable_reason = _availability(entry.get('config'))
        if not available:
            skipped.append({'slug': slug,
                            'reason': f'engine unavailable: {unavailable_reason}'})
            engine_blocked.append({'slug': slug,
                                   'unavailable_reason': unavailable_reason})
            continue

        config = dict(entry['config'])
        override = overrides.get(slug) or {}
        if not isinstance(override, dict):
            skipped.append({'slug': slug, 'reason': 'bad overrides'})
            continue
        # The pack matrix on the install screen: per-template tightening
        # before the rows are written. Only keys the builder itself saves —
        # anything else is refused by the serializer below rather than stored.
        for key in ('autonomy', 'writePaths', 'commandScope', 'toolPermissions',
                    'toolScope', 'spendCapRupees', 'playbooks'):
            if key in override:
                config[key] = override[key]
        serializer = AgentSerializer(data=config, context={'request': request})
        if not serializer.is_valid():
            skipped.append({'slug': slug, 'reason': 'invalid configuration'})
            continue

        base_name = serializer.validated_data['name']
        name = base_name
        counter = 1
        while SubAgent.objects.filter(user=request.user, name=name).exists():
            name = f'{base_name} ({counter})'
            counter += 1
        data = dict(serializer.validated_data, name=name)
        with transaction.atomic():
            agent = AgentSerializer.apply(SubAgent(user=request.user), data)
            agent.tags = list(entry.get('tags') or []) + [f'template:{slug}']
            agent.icon = entry.get('icon', '')
            agent.template_slug = slug
            agent.save()
            AgentSerializer.sync_schedule(agent, data)
            revisions.record(agent, user=request.user, source='create')
        installed.append({'slug': slug, 'id': agent.id, 'name': agent.name})

    if pack == 'code':
        _wire_coding_lead(request.user)

    if not installed and engine_blocked and not any(
        s['reason'] in ('already installed', 'needs setup', 'no such template',
                        'bad overrides', 'invalid configuration')
        for s in skipped
    ):
        # Every member that could install is engine-blocked: an empty 200
        # would read as "done" while installing nothing, so this is a 409
        # naming the engine, like the single-template install.
        return Response(
            {'error': f'Pack "{pack}" cannot run on this server. '
                      f"{engine_blocked[0]['unavailable_reason']}",
             'unavailable_reason': engine_blocked[0]['unavailable_reason'],
             'blocked': engine_blocked},
            status=status.HTTP_409_CONFLICT,
        )

    return Response({'pack': pack, 'installed': installed, 'skipped': skipped})


def _wire_coding_lead(user) -> None:
    """Point the installed coding lead at its installed roster.

    `coding-lead` gets `delegatesTo` set to the other code agents at install
    time, through the existing delegation scope — so a lead cannot delegate
    outside the roster. Templates travel without ids, so this cannot live in
    `agents/gallery/`: it resolves slugs to the caller's own rows after install
    (including rows from an earlier install — reinstalling only adds what is
    missing, and the scope is recomputed over all of it).
    """
    roster = [s for s in (gallery.PACKS.get('code') or [])
              if s not in ('coding-lead', 'repo-assistant')]
    lead = SubAgent.objects.filter(user=user, template_slug='coding-lead').first()
    if lead is None:
        return
    ids = list(SubAgent.objects.filter(
        user=user, template_slug__in=roster).values_list('id', flat=True))
    ids = sorted(i for i in ids if i != lead.id)
    ctx = dict(lead.agent_context or {})
    if ctx.get('delegatesTo') != ids:
        ctx['delegatesTo'] = ids
        lead.agent_context = ctx
        lead.save(update_fields=['agent_context', 'updated_at'])


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


# --------------------------------------------------------------- public reads
#
# The second unauthenticated surface in this app, after the webhook receiver,
# and it follows that route's rules because the reasoning is the same.
#
# **404 for everything that is not public.** A `link` share, a `platform`
# share, a withdrawn one and a slug that never existed are indistinguishable
# from outside. Anything else turns this into an oracle for enumerating what
# people have published privately — the exact failure `webhook_receive`
# documents.
#
# **A narrower projection, not the same one.** `_present_public` is its own
# function rather than `_present_shared` with a flag: the signed-in shape
# carries `is_mine` and per-requirement `candidates`, both of which are
# computed from a caller that does not exist here, and a flag on one function
# is how the account-shaped fields eventually leak into the anonymous
# response.
#
# **Bounded.** DRF's `DEFAULT_PAGINATION_CLASS` never applies to `@api_view`
# function views, so the cap is this module's to set — the rule the rest of
# this codebase states as "nothing returns an unbounded list".
#
# Throttling *is* inherited: `DEFAULT_THROTTLE_CLASSES` applies to function
# views, so `AnonRateThrottle` (100/hour) covers these without further wiring.


def _present_public(share: SharedAgent) -> dict:
    """One published agent, as somebody with no account may see it.

    Deliberately absent: `candidates` (there is no caller whose rows could fill
    them), `is_mine`, and anything counting the author's own activity. What is
    kept is what a stranger needs to decide whether to sign up for it — what it
    does, what it would be able to reach, and what they would have to supply.
    """
    return {
        'slug': share.slug,
        'source': 'community',
        'name': share.name,
        'tagline': share.tagline,
        'description': share.description,
        'icon': share.icon,
        'tags': share.tags or [],
        'author': _author_name(share.author),
        'install_count': share.install_count,
        'version': share.version,
        'updated_at': share.updated_at,
        # The kinds only — the labels and the `why`, without any pool to pick
        # from. It is what tells a visitor "you will need a mailbox for this".
        'requirements': [
            {k: v for k, v in req.items() if k != 'candidates'}
            for req in (share.requirements or [])
        ],
        'config': share.config,
        # Server state, not caller state, so the anonymous projection may
        # carry it: a visitor deciding whether to sign up wants to know.
        'available': _availability(share.config or {})[0],
    }


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description='Publicly shared agents.')},
    description='Agents published for anyone, including people without an '
                'account. No authentication.',
    auth=[],
)
@api_view(['GET'])
@permission_classes([AllowAny])
def public_agent_list(request):
    shares = (
        SharedAgent.objects
        .filter(is_listed=True, visibility='public')
        .select_related('author')
        .order_by('-install_count', '-updated_at')[:PUBLIC_CATALOGUE_LIMIT + 1]
    )
    shares = list(shares)
    truncated = len(shares) > PUBLIC_CATALOGUE_LIMIT
    return Response({
        'results': [_present_public(s) for s in shares[:PUBLIC_CATALOGUE_LIMIT]],
        # A capped list and a complete one must not look alike.
        'truncated': truncated,
    })


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description='One publicly shared agent.'),
               404: OpenApiResponse(description='No public agent at this slug.')},
    description='One publicly shared agent. No authentication.',
    auth=[],
)
@api_view(['GET'])
@permission_classes([AllowAny])
def public_agent_detail(request, slug: str):
    share = (SharedAgent.objects.select_related('author')
             .filter(slug=slug, is_listed=True, visibility='public')
             .first())
    if share is None:
        # Deliberately the same answer for "never existed", "not public",
        # "withdrawn" and "link-only". See the note above this section.
        return Response({'error': 'Not found.'},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(_present_public(share))
