"""
Mission tools: only offered inside a mission run.

`mission_status` reads; `wait_for` parks the mission on an event;
`complete_mission` closes it; `report_progress` writes the timeline (and, at
most hourly, a notification). Starting a mission from chat is the `sensitive`
`start_mission`, like `create_agent`: a person approves the goal, budget and
deadline.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Dict

from django.utils import timezone

from .registry import tool

logger = logging.getLogger(__name__)


def _mission(context: Dict):
    mission_id = context.get('mission_id')
    if not mission_id:
        return None
    from missions.models import Mission

    return Mission.objects.filter(id=mission_id).first()


@tool({
    'type': 'function',
    'function': {
        'name': 'mission_status',
        'description': 'Read the mission: goal, plan, budget used, runs done.',
        'parameters': {
            'type': 'object', 'properties': {},
            'additionalProperties': False,
        },
    },
}, effect='read')
async def mission_status(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async

    mission = await sync_to_async(_mission)(context)
    if mission is None:
        return json.dumps({'error': 'This run is not part of a mission.'})
    return json.dumps({
        'goal': mission.goal, 'status': mission.status,
        'plan': mission.plan, 'runs_done': mission.runs_done,
        'max_runs': mission.max_runs,
        'budget_inr': mission.budget_inr, 'spent_inr': mission.spent_inr,
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'wait_for',
        'description': (
            'Park the mission until an event arrives (message.received, '
            'job.finished, email.received, esign.completed, time). The run '
            'ends; the mission wakes on the event or the timeout.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'event': {'type': 'string'},
                'filter': {'type': 'object', 'additionalProperties': True},
                'timeout': {'type': 'integer', 'description': 'Seconds to wait.'},
            },
            'required': ['event'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def wait_for(args: Dict, context: Dict) -> str:
    event = str(args.get('event') or '').strip()
    if not event:
        return json.dumps({'error': 'Give the event to wait for.'})
    return json.dumps({
        'waiting': True, 'event': event,
        'filter': args.get('filter') or {},
        'timeout': int(args.get('timeout') or 86400),
        'rendered': f'Waiting on {event}. The run ends here.',
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'complete_mission',
        'description': 'Close the mission with a summary of what was done.',
        'parameters': {
            'type': 'object',
            'properties': {
                'summary': {'type': 'string'},
                'outputs': {'type': 'array', 'items': {'type': 'string'}},
            },
            'required': ['summary'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def complete_mission(args: Dict, context: Dict) -> str:
    summary = str(args.get('summary') or '').strip()
    if not summary:
        return json.dumps({'error': 'Give the summary of what was done.'})
    return json.dumps({
        'completed': True, 'summary': summary,
        'outputs': args.get('outputs') or [],
        'rendered': 'Mission complete.',
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'report_progress',
        'description': 'Write to the mission timeline (and, at most hourly, notify the user).',
        'parameters': {
            'type': 'object',
            'properties': {
                'text': {'type': 'string', 'description': 'What to report.'},
            },
            'required': ['text'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def report_progress(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async

    text = str(args.get('text') or '').strip()[:2000]
    if not text:
        return json.dumps({'error': 'Give the text to report.'})
    mission = await sync_to_async(_mission)(context)
    if mission is None:
        return json.dumps({'error': 'This run is not part of a mission.'})

    def _save():
        mission.last_report = text
        mission.save(update_fields=['last_report', 'updated_at'])

    await sync_to_async(_save)()
    return json.dumps({'reported': True})


@tool({
    'type': 'function',
    'function': {
        'name': 'start_mission',
        'description': (
            'Start a multi-run mission: a goal pursued across runs until done, '
            'waiting, or out of budget. The user approves the goal, budget and '
            'deadline before anything starts.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'agent_id': {'type': 'integer'},
                'goal': {'type': 'string'},
                'budget_inr': {'type': 'integer'},
                'deadline_days': {'type': 'integer'},
                'max_runs': {'type': 'integer'},
            },
            'required': ['agent_id', 'goal', 'budget_inr'],
            'additionalProperties': False,
        },
    },
}, sensitive=True, effect='reversible')
async def start_mission(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        agent_id = int(args.get('agent_id'))
    except (TypeError, ValueError):
        return json.dumps({'error': 'Give the numeric agent_id to run the mission with.'})
    goal = str(args.get('goal') or '').strip()
    if not goal:
        return json.dumps({'error': 'Give the mission goal.'})
    try:
        budget = int(args.get('budget_inr'))
    except (TypeError, ValueError):
        return json.dumps({'error': '`budget_inr` is required: the most the mission may spend.'})
    if budget <= 0:
        return json.dumps({'error': '`budget_inr` must be positive.'})
    try:
        max_runs = max(1, min(int(args.get('max_runs') or 20), 100))
    except (TypeError, ValueError):
        max_runs = 20
    try:
        days = max(1, min(int(args.get('deadline_days') or 7), 90))
    except (TypeError, ValueError):
        days = 7

    def _create():
        from agents.models import SubAgent

        from missions.models import Mission

        agent = SubAgent.objects.filter(id=agent_id, user_id=user_id).first()
        if agent is None:
            raise ValueError(f'No agent {agent_id} belongs to this user.')
        return Mission.objects.create(
            user_id=user_id, agent=agent, goal=goal,
            budget_inr=budget, max_runs=max_runs,
            deadline=timezone.now() + timedelta(days=days),
            next_wake_at=timezone.now(),
        )

    try:
        row = await sync_to_async(_create)()
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Missions] start_mission failed')
        return json.dumps({'error': 'The mission could not be started.'})
    return json.dumps({
        'mission_id': row.id, 'status': 'active',
        'rendered': f'Started mission {row.id}.',
    })


MISSION_TOOLS = ('mission_status', 'wait_for', 'complete_mission',
                 'report_progress', 'start_mission')
