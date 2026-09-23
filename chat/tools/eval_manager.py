"""
Eval management from chat (EVAL_EXPANSION_PLAN §6).

Chat-only tools like `authoring.py`: in no `GRANT_TOOLS` value and not in
`ALWAYS_AVAILABLE`, so an agent run cannot reach them at any autonomy level.
Without that, an agent holding `subAgents` could mint eval suites that spend
the owner's credits unattended. Reads are plain; writes and runs are
`sensitive` so the user approves before anything is created or spent.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from asgiref.sync import sync_to_async

from .registry import tool

logger = logging.getLogger(__name__)


async def _user(context: Dict):
    from django.contrib.auth import get_user_model
    return await get_user_model().objects.filter(id=context.get('user_id')).afirst()


@tool({
    "type": "function",
    "function": {
        "name": "list_eval_suites",
        "description": (
            "List this user's eval suites with case counts and last run. "
            "Use it when the user asks 'is my agent any good' or 'what evals "
            "do I have' before proposing anything."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}, effect="read")
async def list_eval_suites(args: Dict, context: Dict) -> str:
    user = await _user(context)
    if user is None:
        return json.dumps({'error': 'No user in context.'})

    def read():
        from eval import queries
        suites = list(queries.suites_for(user))
        health = {h['id']: h for h in queries.suite_health(user)}
        out = []
        for s in suites:
            h = health.get(s.id, {})
            out.append({
                'id': s.id, 'name': s.name,
                'cases': h.get('case_count', s.cases.filter(is_active=True).count()),
                'supervision': s.supervision,
                'template': s.template_slug,
            })
        return out

    return json.dumps({'suites': await sync_to_async(read)()})


@tool({
    "type": "function",
    "function": {
        "name": "get_scorecard",
        "description": (
            "How one agent scores across every suite pointed at it: latest "
            "settled 0-100 score per suite plus history. Use it to answer "
            "'how is my agent doing' with numbers, never from memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "The agent to score."},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
}, effect="read")
async def get_scorecard(args: Dict, context: Dict) -> str:
    user = await _user(context)
    if user is None:
        return json.dumps({'error': 'No user in context.'})
    try:
        agent_id = int(args.get('agent_id'))
    except (TypeError, ValueError):
        return json.dumps({'error': 'Give the numeric agent_id.'})

    def read():
        from agents.models import SubAgent
        from eval import queries
        agent = SubAgent.objects.filter(id=agent_id, user=user).first()
        if agent is None:
            return {'error': 'No such agent.'}
        return {'agent': agent.name, 'suites': queries.agent_scorecard(user, agent.id)}

    return json.dumps(await sync_to_async(read)(), default=str)


@tool({
    "type": "function",
    "function": {
        "name": "create_eval_suite_from_starter",
        "description": (
            "Create an eval suite from a starter kit (research, analyst, "
            "files, support, code) for one of the user's agents. Ask which "
            "agent and which kit first; a vague request makes a useless "
            "suite. The user approves before anything is created."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "template": {"type": "string", "description": "Starter kit slug: research, analyst, files, support, code."},
                "name": {"type": "string", "description": "Suite name. Defaults to the kit name."},
                "agent_id": {"type": "integer", "description": "Agent to point the suite at."},
            },
            "required": ["template"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible")
async def create_eval_suite_from_starter(args: Dict, context: Dict) -> str:
    user = await _user(context)
    if user is None:
        return json.dumps({'error': 'No user in context.'})

    def write():
        from agents.models import SubAgent
        from eval import api as evals
        agent = None
        if args.get('agent_id'):
            agent = SubAgent.objects.filter(id=int(args['agent_id']), user=user).first()
            if agent is None:
                return {'error': 'No such agent.'}
        try:
            suite = evals.clone_starter_kit(
                user=user, template=str(args.get('template', '')),
                name=str(args.get('name') or ''), agent=agent)
        except evals.GraderError as exc:
            return {'error': str(exc)}
        return {'suite_id': suite.id, 'name': suite.name,
                'cases': suite.cases.filter(is_active=True).count()}

    try:
        return json.dumps(await sync_to_async(write)())
    except Exception as exc:  # noqa: BLE001
        logger.exception('[EvalManager] clone failed')
        return json.dumps({'error': f'Could not create the suite: {exc}'})


@tool({
    "type": "function",
    "function": {
        "name": "add_eval_case",
        "description": (
            "Add one case to a suite: goal (required), what-good-looks-like "
            "reference, and graders. A case with only an LLM judge is refused "
            "— pair it with one exact check. Find the suite id with "
            "list_eval_suites first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "suite_id": {"type": "integer"},
                "name": {"type": "string"},
                "goal": {"type": "string", "description": "The prompt the agent is run against."},
                "reference": {"type": "string", "description": "What a good answer looks like."},
                "graders": {"type": "array", "items": {"type": "object"},
                            "description": 'E.g. [{"type": "contains", "value": "30 days"}]'},
            },
            "required": ["suite_id", "goal"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible")
async def add_eval_case(args: Dict, context: Dict) -> str:
    user = await _user(context)
    if user is None:
        return json.dumps({'error': 'No user in context.'})

    def write():
        from eval import graders as _graders
        from eval.models import EvalCase, EvalSuite
        try:
            suite = EvalSuite.objects.filter(id=int(args.get('suite_id')), user=user).first()
        except (TypeError, ValueError):
            return {'error': 'Give the numeric suite_id.'}
        if suite is None:
            return {'error': 'No such suite.'}
        try:
            validated = _graders.validate_case_graders(args.get('graders') or [])
        except _graders.GraderError as exc:
            return {'error': str(exc)}
        order = suite.cases.count()
        case = EvalCase.objects.create(
            suite=suite, order=order,
            name=str(args.get('name') or f'Case {order + 1}')[:200],
            goal=str(args.get('goal') or ''),
            reference=str(args.get('reference') or ''),
            graders=validated,
        )
        return {'case_id': case.id, 'suite_id': suite.id, 'name': case.name}

    try:
        return json.dumps(await sync_to_async(write)())
    except Exception as exc:  # noqa: BLE001
        logger.exception('[EvalManager] add case failed')
        return json.dumps({'error': f'Could not add the case: {exc}'})


@tool({
    "type": "function",
    "function": {
        "name": "run_eval_suite",
        "description": (
            "Run a suite against an agent and return the run id. A sweep is "
            "one agent run per case and spends credits — the user approves "
            "before it starts. Poll the Evals page for the 0-100 scorecard."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "suite_id": {"type": "integer"},
                "agent_id": {"type": "integer", "description": "Defaults to the suite's agent."},
            },
            "required": ["suite_id"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible")
async def run_eval_suite(args: Dict, context: Dict) -> str:
    user = await _user(context)
    if user is None:
        return json.dumps({'error': 'No user in context.'})

    def load():
        from agents.models import SubAgent
        from eval.models import EvalSuite
        try:
            suite = EvalSuite.objects.filter(id=int(args.get('suite_id')), user=user).first()
        except (TypeError, ValueError):
            return None, None, 'Give the numeric suite_id.'
        if suite is None:
            return None, None, 'No such suite.'
        agent = suite.subagent
        if args.get('agent_id'):
            agent = SubAgent.objects.filter(id=int(args['agent_id']), user=user).first()
            if agent is None:
                return None, None, 'No such agent.'
        if agent is None:
            return None, None, 'This suite names no agent. Pass agent_id.'
        return suite, agent, ''

    suite, agent, error = await sync_to_async(load)()
    if error:
        return json.dumps({'error': error})
    try:
        from eval.runner import start_suite_run
        run_id = await start_suite_run(suite, agent, user)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({'error': str(exc)[:500]})
    return json.dumps({'run_id': run_id, 'suite_id': suite.id, 'agent_id': agent.id})


__all__ = ['list_eval_suites', 'get_scorecard', 'create_eval_suite_from_starter',
           'add_eval_case', 'run_eval_suite']
