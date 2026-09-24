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
            "Draft one case for a suite: goal (required), what-good-looks-like "
            "reference, and graders. Saves a DRAFT — it scores nothing until "
            "the owner accepts it on the Evals page, so say that and link "
            "there. A case with only an LLM judge is refused — pair it with "
            "one exact check. Find the suite id with list_eval_suites first."
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
        from eval import api as evals
        from eval.models import EvalSuite
        try:
            suite = EvalSuite.objects.filter(id=int(args.get('suite_id')), user=user).first()
        except (TypeError, ValueError):
            return {'error': 'Give the numeric suite_id.'}
        if suite is None:
            return {'error': 'No such suite.'}
        try:
            saved = evals.save_cases(suite, [{
                'name': str(args.get('name') or ''),
                'goal': str(args.get('goal') or ''),
                'reference': str(args.get('reference') or ''),
                'graders': args.get('graders') or [],
            }], drafts=True)
        except evals.GraderError as exc:
            return {'error': str(exc)}
        if not saved:
            return {'error': 'The suite is full.'}
        case = saved[0]
        return {'case_id': case.id, 'suite_id': suite.id, 'name': case.name,
                'draft': True,
                'review': 'Accept it on the Evals page before it scores anything.'}

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


@tool({
    "type": "function",
    "function": {
        "name": "create_eval_suite",
        "description": (
            "Create an empty eval suite pointed at one of the user's agents. "
            "The user approves before anything is created. Follow it with "
            "generate_eval_world ('test my invoice agent') or add_eval_case, "
            "then link the Evals page for review."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "Agent this suite tests."},
                "name": {"type": "string", "description": "Suite name. Defaults to '<agent> evals'."},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible")
async def create_eval_suite(args: Dict, context: Dict) -> str:
    user = await _user(context)
    if user is None:
        return json.dumps({'error': 'No user in context.'})

    def write():
        from agents.models import SubAgent
        from eval.models import EvalSuite
        try:
            agent = SubAgent.objects.filter(
                id=int(args.get('agent_id')), user=user).first()
        except (TypeError, ValueError):
            return {'error': 'Give the numeric agent_id.'}
        if agent is None:
            return {'error': 'No such agent.'}
        base = str(args.get('name') or f'{agent.name} evals').strip()[:200] or 'Evals'
        name, n = base, 2
        while EvalSuite.objects.filter(user=user, name=name).exists():
            name = f'{base} ({n})'
            n += 1
        suite = EvalSuite.objects.create(
            user=user, name=name, subagent=agent,
            description=f'Evaluating {agent.name}.')
        return {'suite_id': suite.id, 'name': suite.name,
                'agent_id': agent.id, 'agent': agent.name}

    try:
        return json.dumps(await sync_to_async(write)())
    except Exception as exc:  # noqa: BLE001
        logger.exception('[EvalManager] create suite failed')
        return json.dumps({'error': f'Could not create the suite: {exc}'})


@tool({
    "type": "function",
    "function": {
        "name": "generate_eval_world",
        "description": (
            "Judge-build a fake world for a suite's agent plus test cases "
            "about it: the judge invents a situation, plants facts, builds "
            "fixtures holding them, and writes cases with known answers. "
            "About 5 judge-model calls for up to 25 cases, billed to the "
            "user's key. Everything arrives as DRAFTS — nothing scores until "
            "the owner accepts the world and its cases on the Evals page, so "
            "reply with that link afterwards, never with a score."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "suite_id": {"type": "integer"},
                "focus": {"type": "string",
                          "description": 'What to aim the world at, e.g. "month-end close with duplicate invoices".'},
                "cases": {"type": "integer",
                          "description": "How many cases (1-25, default 12)."},
            },
            "required": ["suite_id"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible")
async def generate_eval_world(args: Dict, context: Dict) -> str:
    user = await _user(context)
    if user is None:
        return json.dumps({'error': 'No user in context.'})
    try:
        from eval.models import EvalSuite
        suite = await EvalSuite.objects.select_related('subagent').filter(
            id=int(args.get('suite_id')), user=user).afirst()
    except (TypeError, ValueError):
        return json.dumps({'error': 'Give the numeric suite_id.'})
    if suite is None:
        return json.dumps({'error': 'No such suite.'})
    if suite.subagent_id is None:
        return json.dumps({'error': 'Point the suite at an agent first.'})
    try:
        from eval import api as evals
        from eval.generator import generate_world
        from llm.access import LLMUserActionable
        out = await generate_world(
            suite.subagent, user_id=user.id,
            focus=str(args.get('focus') or ''),
            cases=int(args.get('cases') or 12))
    except LLMUserActionable as exc:
        return json.dumps({'error': str(exc)})
    except ValueError as exc:
        return json.dumps({'error': f'The generator reply could not be used: {exc}'})
    except Exception as exc:  # noqa: BLE001 - provider down
        logger.warning('[EvalManager] world generation failed: %s', exc)
        return json.dumps({'error': f'Generation failed: {exc}'})

    def save():
        return evals.save_generated_world(suite, out)

    try:
        world, saved = await sync_to_async(save)()
    except Exception as exc:  # noqa: BLE001
        logger.exception('[EvalManager] world save failed')
        return json.dumps({'error': f'Could not save the world: {exc}'})
    return json.dumps({
        'world_id': world.id, 'version': world.version,
        'brief': world.brief,
        'cases': len(saved), 'rejected': out['rejected'],
        'cost_usd': out['cost_usd'],
        'review': ('Nothing scores until review: accept the world, then its '
                   'cases, on the Evals page.'),
    })


@tool({
    "type": "function",
    "function": {
        "name": "import_eval_cases_from_runs",
        "description": (
            "Draft eval cases from an agent's recent real runs: the goal is "
            "what was really asked. Saves DRAFTS — nothing scores until the "
            "owner accepts them on the Evals page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "suite_id": {"type": "integer"},
                "source": {"type": "string",
                           "description": "all, rated or thumbs_down (default all)."},
                "limit": {"type": "integer",
                          "description": "How many (default 20)."},
            },
            "required": ["suite_id"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible")
async def import_eval_cases_from_runs(args: Dict, context: Dict) -> str:
    user = await _user(context)
    if user is None:
        return json.dumps({'error': 'No user in context.'})

    def write():
        from eval import api as evals
        from eval.generator import import_candidates
        from eval.models import EvalSuite
        try:
            suite = EvalSuite.objects.filter(
                id=int(args.get('suite_id')), user=user).first()
        except (TypeError, ValueError):
            return {'error': 'Give the numeric suite_id.'}
        if suite is None:
            return {'error': 'No such suite.'}
        if suite.subagent_id is None:
            return {'error': 'Point the suite at an agent first.'}
        try:
            drafts, skipped = import_candidates(
                user, suite, source=str(args.get('source') or 'all'),
                limit=args.get('limit') or 20)
        except ValueError as exc:
            return {'error': str(exc)}
        tagged = []
        for draft in drafts:
            tags = ['from-run', 'needs-review',
                    draft.get('execution_id', ''), draft.get('source', '')]
            tagged.append({**draft, 'tags': [t for t in tags if t]})
        saved = evals.save_cases(suite, tagged, drafts=True)
        return {'cases': len(saved), 'already_imported': skipped,
                'draft': True,
                'review': 'Accept them on the Evals page before they score.'}

    try:
        return json.dumps(await sync_to_async(write)())
    except Exception as exc:  # noqa: BLE001
        logger.exception('[EvalManager] import runs failed')
        return json.dumps({'error': f'Could not import runs: {exc}'})


@tool({
    "type": "function",
    "function": {
        "name": "get_eval_results",
        "description": (
            "Explain one eval sweep, case by case: pass or fail, which check "
            "decided, what the run wanted from a person, and what it changed "
            "in the world. Use it to answer 'why did it fail' and to propose "
            "an agent fix — proposing still goes through update_agent, which "
            "asks for approval separately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The sweep id."},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
}, effect="read")
async def get_eval_results(args: Dict, context: Dict) -> str:
    user = await _user(context)
    if user is None:
        return json.dumps({'error': 'No user in context.'})

    def read():
        from eval import queries
        run, results, _meta = queries.run_with_results(
            user, str(args.get('run_id') or ''))
        if run is None:
            return {'error': 'No such run.'}
        out = []
        for result in results:
            grades = result.grades or []
            failed = [g for g in grades if not g.get('passed', True)]
            out.append({
                'case': result.case_name or f'case {result.case_id}',
                'status': result.status,
                'passed': result.final_passed,
                'failing': [f"{g.get('type')}: {str(g.get('detail') or '')[:200]}"
                            for g in failed][:3],
                'answer': (result.answer or '')[:500],
                'intents': result.intents or [],
                'changed': result.env_changes or {},
                'error': (result.error_message or '')[:300],
            })
        return {'run_id': str(run.run_id), 'status': run.status,
                'score': run.score, 'passed': run.passed,
                'cases': out[:50], 'truncated': len(results) > 50}

    try:
        return json.dumps(await sync_to_async(read)(), default=str)
    except Exception as exc:  # noqa: BLE001
        logger.exception('[EvalManager] results failed')
        return json.dumps({'error': f'Could not read the run: {exc}'})


__all__ = ['list_eval_suites', 'get_scorecard', 'create_eval_suite_from_starter',
           'create_eval_suite', 'add_eval_case', 'generate_eval_world',
           'import_eval_cases_from_runs', 'run_eval_suite', 'get_eval_results']
