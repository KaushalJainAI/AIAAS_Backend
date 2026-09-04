"""
Turning one user's agent into something another user can install.

The gallery in `agents/gallery.py` is hand-written and portable by
construction: a human wrote `requirements` and never typed an id. A *published*
agent is the opposite — it is a live configuration full of ids that mean
something only in its author's account, and nothing about saving it made anyone
think about that. So the stripping has to be done by code, at publish time, and
it has to be the same shape the gallery already speaks, or the installer would
need two screens.

`to_shareable` is that translation. Two rules carry it.

**Every id becomes a requirement, or the publish fails.** Not "is dropped" —
dropped is how an agent arrives in a stranger's account silently missing the
corpus it was written around, answering from nothing and looking merely stupid.
Each id is looked up, and it becomes a requirement carrying the *kind* of thing
it was and a label the author can edit before publishing. An id that no longer
resolves is a publish-time error, because the author is present and can fix it.

**Nothing that is not on the publish screen travels.** The wire shape is an
allow-list (`SHAREABLE_KEYS`), not `to_config()` minus a few keys. A denylist
means every field added to `AgentConfig` later is published by default, and the
first one that carries something private is a leak nobody wrote a line of code
to cause. Credentials are not in `AgentConfig` at all (`llm_credential` is a
column), and the allow-list is what keeps that true as the config grows.

The labels are the third thing, and they are a judgement rather than a rule: a
requirement defaults to the source row's own name, because "Q3 payroll" tells
the installer far more about what to plug in than "Knowledge base 1" does — and
it is also a fact about the author's account. That is why the default is shown
on the publish screen, editable, before anything is written: the author sees
exactly what will travel and can rename it. `sanitise_requirements` is what
accepts their edit without letting them change the *kind*, which the installer's
picker is built from.
"""
from __future__ import annotations

from typing import Any

from agents.gallery import REQUIREMENT_FIELDS

#: What a published agent carries. An allow-list, deliberately: see the module
#: docstring. Everything here is a knob the installer is shown and can change,
#: and none of it names a row, a credential, or a run.
#:
#: Absent on purpose: `id`, `status`, `created_at`, `updated_at`, `runs`,
#: `unattended`, `spend` and `extraSchedules` (facts about the author's copy,
#: not about the configuration), and the three id lists, which leave as
#: requirements instead.
SHAREABLE_KEYS: frozenset[str] = frozenset({
    'name', 'brief',
    'provider', 'model', 'temperature',
    'fileAccess', 'workdir', 'venv',
    'tools',
    'useOrgContext', 'useEnvironment',
    'trigger', 'schedule', 'allowUnattended',
    'autonomy', 'notifyOnHitl', 'reviewAgent', 'spendCapRupees',
    'maxRunSeconds', 'egress',
    'summaryModel', 'summaryProvider',
    'recursiveContext', 'compaction', 'indexing',
})

#: The timezone is deliberately *not* shareable. A schedule means "Monday
#: morning" to its author, and Monday morning is a different instant for
#: whoever installs it; carrying the author's zone would fire a stranger's
#: weekly report at 03:30 with nothing to explain why. The installer's own zone
#: is applied at install, exactly as it is for a curated template.


class PublishError(Exception):
    """The agent cannot be published as it stands, and the author can fix it."""


def _requirement_for(kind: str, row, index: int) -> dict[str, Any]:
    """One id, as the portable thing that replaces it."""
    if kind == 'connector':
        return {
            'key': f'connector_{index}',
            'type': 'connector',
            'label': row.label,
            'why': f'The agent was built against a connection like {row.label}.',
            # The icon slug is the same hint the curated catalogue uses, so the
            # installer's picker orders their own connections the same way.
            'provider': row.icon_slug or '',
            'optional': False,
        }
    if kind == 'knowledge_base':
        return {
            'key': f'knowledge_base_{index}',
            'type': 'knowledge_base',
            'label': row.name,
            'why': 'Documents for the agent to search. Point it at your own.',
            'optional': False,
        }
    return {
        'key': f'skill_{index}',
        'type': 'skill',
        'label': row.title,
        'why': 'A skill the agent was written to use.',
        'optional': True,
    }


def to_shareable(agent) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(config, requirements)` — the agent as something portable.

    Raises `PublishError` when an id on the agent no longer resolves. Silently
    dropping it would publish an agent whose corpus vanished, and the author is
    the only person who can say what it should have been.
    """
    from agents.views.agents import AgentSerializer
    from inference.models import KnowledgeBase
    from mcp_integration.client import visible_servers_sync
    from skills.models import Skill

    full = AgentSerializer.to_config(agent)
    config = {k: v for k, v in full.items() if k in SHAREABLE_KEYS}

    requirements: list[dict[str, Any]] = []

    connector_ids = list(full.get('connectors') or [])
    if connector_ids:
        by_id = {s.id: s for s in visible_servers_sync(agent.user_id)}
        for index, cid in enumerate(connector_ids, start=1):
            row = by_id.get(cid)
            if row is None:
                raise PublishError(
                    f'This agent uses a connection (id {cid}) you can no longer '
                    f'see. Remove it in the builder, then publish.'
                )
            requirements.append(_requirement_for('connector', row, index))

    kb_ids = list(full.get('knowledgeBases') or [])
    if kb_ids:
        by_id = {kb.id: kb for kb in
                 KnowledgeBase.objects.filter(user_id=agent.user_id, id__in=kb_ids)}
        for index, kid in enumerate(kb_ids, start=1):
            row = by_id.get(kid)
            if row is None:
                raise PublishError(
                    f'This agent uses a knowledge base (id {kid}) that no longer '
                    f'exists. Remove it in the builder, then publish.'
                )
            requirements.append(_requirement_for('knowledge_base', row, index))

    skill_ids = list(full.get('skills') or [])
    if skill_ids:
        by_id = {s.id: s for s in
                 Skill.objects.filter(user_id=agent.user_id, id__in=skill_ids)}
        for index, sid in enumerate(skill_ids, start=1):
            row = by_id.get(sid)
            if row is None:
                raise PublishError(
                    f'This agent uses a skill (id {sid}) that no longer exists. '
                    f'Remove it in the builder, then publish.'
                )
            requirements.append(_requirement_for('skill', row, index))

    # The invariant the whole design rests on, asserted rather than assumed:
    # a config that still holds one of these lists would install by silently
    # reading the *installer's* row with the author's id.
    leaked = set(REQUIREMENT_FIELDS.values()) & set(config)
    if leaked:
        raise PublishError(f'Internal: config still carries {sorted(leaked)}.')

    return config, requirements


def sanitise_requirements(
    generated: list[dict[str, Any]], edited: Any
) -> list[dict[str, Any]]:
    """Accept the author's wording; keep everything else as generated.

    The author may rewrite a `label` and a `why`, and may mark a requirement
    optional — those are theirs to describe. They may not change a `key` or a
    `type`, because those are what the installer's picker is built from and
    what install resolves against: an edited type would offer a knowledge-base
    dropdown for something the agent will use as a connection.
    """
    if not isinstance(edited, list):
        return generated

    by_key = {}
    for item in edited:
        if isinstance(item, dict) and isinstance(item.get('key'), str):
            by_key[item['key']] = item

    out = []
    for req in generated:
        override = by_key.get(req['key'], {})
        label = str(override.get('label') or req['label']).strip()[:200]
        why = str(override.get('why') or req['why']).strip()[:400]
        out.append({
            **req,
            'label': label or req['label'],
            'why': why or req['why'],
            'optional': bool(override.get('optional', req['optional'])),
        })
    return out
