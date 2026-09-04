"""
Creating and editing agents by describing them.

The whole surface is two tools, and both go through `AgentSerializer` — the
same validator, the same `apply`, the same `sync_schedule`, the same revision
record as the builder screen. That is not tidiness. A second write path is a
second place to forget the ownership check on knowledge bases, connections and
delegation targets, and those checks are the only reason an agent config is
safe to accept from something that is not a person.

Two containment decisions worth stating, because both are load-bearing and
neither is visible from the tool schemas:

**These are chat tools and nothing else.** They appear in no `GRANT_TOOLS`
value and not in `ALWAYS_AVAILABLE`, so an agent run cannot reach them at any
autonomy level. Without that, an agent holding `subAgents` could mint a *new*
agent holding grants it had itself been refused and then delegate to it, which
turns every grant into a suggestion. The depth and budget bounds on delegation
would still hold; the permission bounds would not.

**They are `sensitive`, so they pause for approval.** The user sees the whole
proposed configuration — its grants included — in the approval prompt before
anything is written, which is what makes "the model chose the capabilities"
acceptable: it proposed them, a person granted them. This reuses the existing
gate rather than inventing a confirmation flow, and it means an agent created
this way went through the same consent step as one built by hand.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from asgiref.sync import sync_to_async

from .registry import tool

logger = logging.getLogger(__name__)

#: Fields a model may set. Deliberately a fraction of what `AgentSerializer`
#: accepts: everything here is something a person can describe in a sentence.
#: The rest — spend caps, run limits, context-lifecycle toggles, output
#: contracts — keeps its default, because a model has no basis for choosing a
#: spend cap and a wrong one is either a surprise bill or an agent that dies
#: mid-run. They stay the builder's to set.
WRITABLE = (
    'name', 'description', 'brief', 'tools', 'autonomy',
    'knowledgeBases', 'skills', 'model', 'provider', 'schedule',
    'scheduleTimezone', 'allowUnattended', 'fileAccess',
)

#: `allowUnattended` is in that list and it is the one entry that deserves an
#: argument, because it is the switch that lets a run happen with nobody
#: watching — off on every row until someone turns it on.
#:
#: Two things make it safe to expose here and neither would be enough alone.
#: The call is `sensitive`, so the user sees the whole configuration, this flag
#: included, and approves it before a row exists. And `AgentSerializer` already
#: refuses a schedule without it — the two are validated as a pair — so there
#: is no way to approve a scheduled agent while believing it will not run
#: unattended. Withholding the flag instead would not have been safer, only
#: broken: every scheduled agent asked for through chat would fail validation,
#: and the user would be told to go and finish the job in the builder.


def _config_schema(required: list[str]) -> dict:
    # From the runtime's own tables rather than `agents.views.agents.TOOL_KEYS`,
    # for two reasons. Importing a views module at decoration time pulls in DRF
    # and therefore the app registry, and this module is imported while Django
    # is still starting up. And `TOOL_KEYS` is `GRANT_TOOLS | UNSERVED_GRANTS`
    # — it includes `shell`, which the runtime refuses to serve, so offering it
    # here would let a model grant a capability that can only ever disappoint.
    # `GRANT_TOOLS` alone is exactly the set that does something.
    from agents.agent.runtime import AUTONOMY_LADDER, GRANT_TOOLS

    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short, specific name. What the agent is for, not a greeting.",
            },
            "description": {
                "type": "string",
                "description": (
                    "One sentence on what it does. This is what a delegating "
                    "agent reads to decide whether to hand it work, so make it "
                    "say what the agent is good for."
                ),
            },
            "brief": {
                "type": "string",
                "description": (
                    "The agent's standing instructions — its system prompt. Be "
                    "specific about what it should do, how it should decide, "
                    "and what it should return."
                ),
            },
            "tools": {
                "type": "object",
                "description": (
                    "Capabilities to grant, as {name: true}. Grant only what "
                    "the brief actually needs — every extra one is reach the "
                    "user did not ask for. Available: "
                    + ", ".join(sorted(GRANT_TOOLS))
                ),
                "additionalProperties": {"type": "boolean"},
            },
            "autonomy": {
                "type": "string",
                "enum": list(AUTONOMY_LADDER),
                "description": (
                    "How much it asks before acting. plan = look and report "
                    "only, review = ask before every tool, ask = ask before "
                    "anything with a side effect, auto = act but ask before "
                    "anything irreversible, full = never ask. Default to 'ask' "
                    "unless the user says otherwise."
                ),
            },
            "knowledgeBases": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Knowledge base ids it may search. Only ids the user owns.",
            },
            "skills": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Skill ids to attach. Only ids the user owns.",
            },
            "model": {
                "type": "string",
                "description": "Model id. Leave out to use the user's default.",
            },
            "provider": {
                "type": "string",
                "description": "Provider slug. Leave out to use the user's default.",
            },
            "schedule": {
                "type": "string",
                "description": (
                    "Five-field cron expression to run it automatically, read "
                    "in scheduleTimezone. Leave out for an agent the user runs "
                    "by hand. If you set one you must also set allowUnattended, "
                    "and you should say plainly that the agent will run on its "
                    "own from then on."
                ),
            },
            "scheduleTimezone": {
                "type": "string",
                "description": "IANA zone the schedule is read in, e.g. 'Asia/Kolkata'.",
            },
            "allowUnattended": {
                "type": "boolean",
                "description": (
                    "Allow this agent to run with nobody watching. Required "
                    "for a schedule to fire — set both together or neither. "
                    "Do not set it on an agent the user will only run by hand."
                ),
            },
            "fileAccess": {
                "type": "string",
                "description": (
                    "Which files it may touch, if it has the fileOps "
                    "capability: none, readonly, scoped (its own folder), "
                    "read_all_write_own, or full."
                ),
            },
        },
        "required": required,
        "additionalProperties": False,
    }


def _clean(payload: Any) -> dict:
    """Keep only the fields a model may set, dropping anything else silently.

    Silently because the alternative is worse: refusing the whole call over an
    extra key the model invented costs a turn and teaches it nothing, while the
    serializer will still reject anything that matters. This only decides which
    of *our* knobs a model may reach.
    """
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k in WRITABLE}


def _save(user, config: dict, agent_id: int | None) -> dict:
    """Create or update one agent through the serializer. Sync; called off-loop.

    Mirrors `agents/views/agents.py` step for step — validate, uniquify the
    name, `apply`, save, `sync_schedule`, `revisions.record` — because those
    steps are the contract, not the view's private business. Skipping
    `sync_schedule` would accept a cron expression and never arm it; skipping
    the revision would make the agent's first edit look like it invented the
    whole configuration.
    """
    from agents.models import SubAgent
    from agents.views.agents import AgentSerializer
    from logs import revisions

    if agent_id is not None:
        agent = SubAgent.objects.filter(id=agent_id, user=user).first()
        if agent is None:
            return {'error': f'No agent {agent_id} belongs to you.'}
        # PATCH semantics, exactly as the detail view does it: merge onto the
        # stored config so an unsent key cannot silently reset a grant.
        merged = {**AgentSerializer.to_config(agent), **config}
        source = 'chat-update'
    else:
        agent = SubAgent(user=user)
        merged = config
        source = 'chat-create'

    serializer = AgentSerializer(data=merged)
    if not serializer.is_valid():
        # Returned to the model rather than raised: these are correctable, and
        # the field errors say exactly what to fix.
        return {'error': 'That configuration was rejected.',
                'details': serializer.errors}

    data = dict(serializer.validated_data)

    if agent_id is None:
        base = data['name']
        name, counter = base, 1
        while SubAgent.objects.filter(user=user, name=name).exists():
            name = f'{base} ({counter})'
            counter += 1
        data['name'] = name

    AgentSerializer.apply(agent, data)
    agent.save()
    AgentSerializer.sync_schedule(agent, data)
    revisions.record(agent, user=user, source=source)

    granted = sorted(k for k, v in (agent.tool_grants or {}).items() if v)
    return {
        'agent_id': agent.id,
        'name': agent.name,
        'granted': granted,
        'autonomy': (agent.guardrails or {}).get('autonomy', 'ask'),
        'created': agent_id is None,
    }


async def _run(context: Dict, config: dict, agent_id: int | None) -> str:
    from django.contrib.auth import get_user_model

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user in context; nothing was saved.'})

    user = await sync_to_async(
        lambda: get_user_model().objects.filter(id=user_id).first()
    )()
    if user is None:
        return json.dumps({'error': 'No user in context; nothing was saved.'})

    try:
        result = await sync_to_async(_save)(user, config, agent_id)
    except Exception as e:  # noqa: BLE001
        logger.exception('[Authoring] Saving an agent failed')
        return json.dumps({'error': f'Could not save the agent: {e}'})
    return json.dumps(result)


@tool({
    "type": "function",
    "function": {
        "name": "create_agent",
        "description": (
            "Create a new saved agent the user can run later or put on a "
            "schedule. Use this when the user describes something they want to "
            "happen repeatedly, or asks you to build them an agent — not for "
            "work you can simply do now. Ask what it should do, what it needs "
            "access to, and how much it should act on its own before calling "
            "this; a vague brief makes a useless agent. Grant only the "
            "capabilities the brief needs. The user is shown the whole "
            "configuration and must approve it before anything is saved."
        ),
        "parameters": _config_schema(required=["name", "brief"]),
    },
}, sensitive=True, effect="reversible")
async def create_agent(args: Dict, context: Dict) -> str:
    return await _run(context, _clean(args), None)


@tool({
    "type": "function",
    "function": {
        "name": "update_agent",
        "description": (
            "Change a saved agent the user already has. Send only the fields "
            "that should change — anything you leave out keeps its current "
            "value. Find the id with `search_agents` first. Note that `tools` "
            "replaces the whole capability set, so include every capability "
            "the agent should end up with, not just the new one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "integer",
                    "description": "The agent to change, from search_agents.",
                },
                **_config_schema(required=[])["properties"],
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible")
async def update_agent(args: Dict, context: Dict) -> str:
    agent_id = args.get('agent_id')
    try:
        agent_id = int(agent_id)
    except (TypeError, ValueError):
        return json.dumps({'error': 'Give the numeric agent_id to change.'})
    return await _run(context, _clean(args), agent_id)
