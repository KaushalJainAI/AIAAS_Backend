"""
The shape an agent's answer is required to come back in.

This is the half of the design that decides whether "configure an agent to do
it" can ever replace "write a tool that does it". `deep_research` is 55 lines
that fan out across queries, read the pages, and hand back
`{type, queries, sources, text}` — and `_on_deep_research` plus the frontend
source panels render exactly that. An agent that can only return prose cannot
stand in for it however good its prompt is, because the contract is what the UI
consumes, not the words.

A **closed registry of named contracts**, not free-form JSON Schema. The set of
shapes the UI can render is closed by construction — there is a panel per
contract or there is not — so a schema language would let an agent declare a
shape nothing can display, which is a promise the product cannot keep. Naming
them also means the agent's prompt can be told what to produce in one line.

Coercion is deliberately forgiving in one direction only: a missing optional
key is filled in, and prose that was supposed to be structured is reported as a
failure rather than silently reshaped. A contract that quietly accepts anything
is the same as having no contract.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ContractError(ValueError):
    """The agent's answer does not satisfy the contract it was given."""


@dataclass(frozen=True, slots=True)
class Contract:
    """One named result shape."""

    name: str
    #: What the model is told to produce. Goes into the agent's system prompt.
    instruction: str
    #: Keys that must be present after coercion.
    required: tuple[str, ...]
    #: key -> default, filled in when absent.
    optional: dict[str, Any]
    #: Last-resort repair for an answer that is close but not exact.
    repair: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _repair_research(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the near-misses a model actually produces for research output."""
    # Models routinely name this `summary` or `content`.
    if 'text' not in payload:
        for alias in ('summary', 'content', 'answer'):
            if alias in payload:
                payload['text'] = payload.pop(alias)
                break
    # A bare list of URLs where objects were asked for.
    sources = payload.get('sources')
    if isinstance(sources, list):
        payload['sources'] = [
            {'url': s, 'title': s} if isinstance(s, str) else s for s in sources
        ]
    return payload


def _repair_files(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the near-misses a model produces for file output."""
    if 'summary' not in payload:
        for alias in ('text', 'content', 'answer'):
            if alias in payload:
                payload['summary'] = payload.pop(alias)
                break
    files = payload.get('files')
    if isinstance(files, str):
        payload['files'] = [files]
    return payload


RESEARCH = Contract(
    name='research',
    instruction=(
        'Return your final answer as a single JSON object and nothing else, '
        'with these keys:\n'
        '  "text"    — your findings in full, as prose with inline reasoning\n'
        '  "queries" — the search queries you actually ran, as a list\n'
        '  "sources" — every page you used, as a list of {"url", "title"}\n'
        'Do not wrap it in a code fence. Do not add commentary around it.'
    ),
    required=('text',),
    optional={'queries': [], 'sources': [], 'type': 'deep_research'},
    repair=_repair_research,
)

EXTRACTION = Contract(
    name='extraction',
    instruction=(
        'Return your final answer as a single JSON object and nothing else, '
        'with these keys:\n'
        '  "rows"    — the extracted records, as a list of objects\n'
        '  "fields"  — the field names present on every row, as a list\n'
        '  "notes"   — anything you could not extract, as prose\n'
        'Do not wrap it in a code fence.'
    ),
    required=('rows',),
    optional={'fields': [], 'notes': '', 'type': 'extraction'},
)

FILES = Contract(
    name='files',
    instruction=(
        'Return your final answer as a single JSON object and nothing else, '
        'with these keys:\n'
        '  "summary" — what you produced, in two or three sentences\n'
        '  "files"   — the workspace paths you wrote, as a list '
        '(e.g. ["/Agents/Analyst/q3-sales.xlsx"])\n'
        'Do not wrap it in a code fence. Do not paste file contents back; '
        'the user opens them as file cards.'
    ),
    required=('summary', 'files'),
    optional={'type': 'files'},
    repair=_repair_files,
)

def _repair_findings(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the near-misses a model produces for review findings."""
    findings = payload.get('findings')
    if isinstance(findings, dict):
        payload['findings'] = [findings]
    items = payload.get('findings')
    if isinstance(items, list):
        cleaned = []
        for item in items:
            if isinstance(item, str):
                cleaned.append({'file': '', 'line': None, 'severity': 'minor',
                                'category': 'readability', 'summary': item,
                                'suggestion': ''})
            elif isinstance(item, dict):
                cleaned.append(item)
        payload['findings'] = cleaned
    return payload


FINDINGS = Contract(
    name='findings',
    instruction=(
        'Return your final answer as a single JSON object and nothing else, '
        'with this key:\n'
        '  "findings" — the issues you found, as a list of '
        '{"file", "line", "severity", "category", "summary", "suggestion"}. '
        'Severity is one of blocker, major, minor, nit; category is one of '
        'correctness, security, performance, readability, tests. '
        'One finding per issue, with a concrete suggestion. '
        'An empty list means the code is clean — say so, do not invent issues.\n'
        'Do not wrap it in a code fence. Do not add commentary around it.'
    ),
    required=('findings',),
    optional={'type': 'findings'},
    repair=_repair_findings,
)

def _repair_code_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the near-misses a model produces for a task plan."""
    tasks = payload.get('tasks')
    if isinstance(tasks, dict):
        payload['tasks'] = [tasks]
    items = payload.get('tasks')
    if isinstance(items, list):
        cleaned = []
        for i, item in enumerate(items):
            if isinstance(item, str):
                cleaned.append({
                    'id': f't{i + 1}', 'title': item[:200], 'agent': '',
                    'instructions': item, 'claims': [], 'reads': [],
                    'depends_on': [], 'acceptance': '',
                })
            elif isinstance(item, dict):
                item = dict(item)
                item.setdefault('id', f't{i + 1}')
                item.setdefault('title', str(item.get('instructions') or '')[:200])
                item.setdefault('agent', '')
                item.setdefault('instructions', item.get('title', ''))
                claims = item.get('claims') or []
                if isinstance(claims, str):
                    claims = [claims]
                item['claims'] = list(claims)
                reads = item.get('reads') or []
                if isinstance(reads, str):
                    reads = [reads]
                item['reads'] = list(reads)
                deps = item.get('depends_on') or item.get('dependsOn') or []
                if isinstance(deps, str):
                    deps = [deps]
                deps = [str(d) for d in deps if str(d) != str(item.get('id'))]
                item['depends_on'] = deps
                # Refuse cycles by dropping edges that close one; the serializer
                # names the cycle in its own refusal for the lead to fix.
                item.setdefault('acceptance', '')
                cleaned.append(item)
        payload['tasks'] = cleaned
    # Drop self-dependencies that slipped past item-level repair.
    ids = {str(t.get('id')) for t in payload.get('tasks', []) if isinstance(t, dict)}
    for t in payload.get('tasks', []):
        if isinstance(t, dict):
            t['depends_on'] = [d for d in t.get('depends_on', []) if d in ids and d != t.get('id')]
    return payload


def _repair_patch(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the near-misses a model produces for a patch report."""
    if 'summary' not in payload:
        for alias in ('text', 'content', 'answer'):
            if alias in payload:
                payload['summary'] = payload.pop(alias)
                break
    changes = payload.get('changes')
    if isinstance(changes, dict):
        payload['changes'] = [changes]
    if isinstance(payload.get('changes'), list):
        cleaned = []
        for item in payload['changes']:
            if isinstance(item, str):
                cleaned.append({'path': item, 'kind': 'edit', 'change_id': None})
            elif isinstance(item, dict):
                item = dict(item)
                item.setdefault('kind', 'edit')
                item.setdefault('change_id', None)
                cleaned.append(item)
        payload['changes'] = cleaned
    tests = payload.get('tests')
    if isinstance(tests, str):
        payload['tests'] = {'command': '', 'passed': False, 'output_tail': tests[-2000:]}
    elif isinstance(tests, dict):
        tests = dict(tests)
        tests.setdefault('command', '')
        tests.setdefault('passed', False)
        tests.setdefault('output_tail', '')
        payload['tests'] = tests
    return payload


CODE_PLAN = Contract(
    name='code_plan',
    instruction=(
        'Return your final answer as a single JSON object and nothing else, '
        'with these keys:\n'
        '  "goal"  — the goal in one sentence\n'
        '  "tasks" — the plan, as a list of {"id", "title", "agent", '
        '"instructions", "claims", "reads", "depends_on", "acceptance"}. '
        '"claims" lists the file globs the task will write and is required '
        'and non-empty for any task whose agent can write. "reads" lists '
        'what it will read. "depends_on" names earlier task ids.\n'
        '  "risks" — what could go wrong, as a list of strings\n'
        'Do not wrap it in a code fence. Do not add commentary around it.'
    ),
    required=('goal', 'tasks'),
    optional={'risks': [], 'type': 'code_plan'},
    repair=_repair_code_plan,
)

PATCH = Contract(
    name='patch',
    instruction=(
        'Return your final answer as a single JSON object and nothing else, '
        'with these keys:\n'
        '  "summary" — what you changed, in two or three sentences\n'
        '  "changes" — the files you touched, as a list of {"path", "kind", '
        '"change_id"} where change_id points at the recorded CodeChange\n'
        '  "tests"   — {"command", "passed", "output_tail"}\n'
        '  "followups" — anything left for someone else, as a list\n'
        'Do not wrap it in a code fence. Do not paste file contents back.'
    ),
    required=('summary', 'changes'),
    optional={'tests': {}, 'followups': [], 'type': 'patch'},
    repair=_repair_patch,
)

#: Name -> contract. Closed on purpose; see the module docstring.
CONTRACTS: dict[str, Contract] = {c.name: c for c in (RESEARCH, EXTRACTION, FILES, FINDINGS, CODE_PLAN, PATCH)}


def get(name: str) -> Contract | None:
    return CONTRACTS.get((name or '').strip().lower())


def resolve(output_schema: dict[str, Any] | None) -> Contract | None:
    """The contract a `SubAgent.output_schema` asks for, if any.

    `{}` means prose, which is the default and always valid.
    """
    if not output_schema:
        return None
    return get(str(output_schema.get('contract', '')))


def instruction_for(contract: Contract | None) -> str:
    """The block appended to the agent's system prompt."""
    if contract is None:
        return ''
    return f'\n\nOUTPUT FORMAT\n{contract.instruction}'


def _strip_fence(text: str) -> str:
    """Models fence JSON despite being told not to. Cheaper to accept than to fight."""
    stripped = text.strip()
    if not stripped.startswith('```'):
        return stripped
    body = stripped.split('\n', 1)[1] if '\n' in stripped else ''
    if body.rstrip().endswith('```'):
        body = body.rstrip()[:-3]
    return body.strip()


def coerce(answer: str, contract: Contract) -> dict[str, Any]:
    """
    Parse and complete an agent's answer against its contract.

    Raises `ContractError` when the answer is not the right shape. That is the
    point: an agent configured to produce research output and returning prose
    has failed at the thing it was configured for, and reporting that is what
    keeps "configuration replaces code" honest. Silently wrapping the prose in
    `{"text": ...}` would make every agent appear to satisfy every contract.
    """
    try:
        payload = json.loads(_strip_fence(answer or ''))
    except (json.JSONDecodeError, TypeError):
        raise ContractError(
            f'Expected a JSON object matching the "{contract.name}" contract, '
            f'got prose.'
        ) from None

    if not isinstance(payload, dict):
        raise ContractError(
            f'Expected a JSON object for the "{contract.name}" contract, '
            f'got {type(payload).__name__}.'
        )

    if contract.repair is not None:
        payload = contract.repair(dict(payload))

    missing = [key for key in contract.required if key not in payload]
    if missing:
        raise ContractError(
            f'The "{contract.name}" contract requires {", ".join(missing)}.'
        )

    for key, default in contract.optional.items():
        payload.setdefault(key, default)

    return payload
