"""
The graders: small, named assertions over what a run produced.

**Registration is the schema.** A grader is one `@grader(...)` declaration
carrying its own parameter list, and `REGISTRY` is what both the API validator
and the runner read — so a grader that can be saved is a grader that can be run,
and adding one means editing one place. This is the same rule `chat/tools/`
follows, for the same reason: the failure it prevents is a spec that validates
against one list and dispatches against another.

**A case passes when every grader passes.** Graders are assertions, not votes,
so there is no per-case threshold to tune — a rubric that needs weighting lives
in a single `llm_judge` with a rubric, not in five assertions averaged together.
`score` is still a weighted mean, because ranking cases by how badly they failed
is useful and because `supervision.py` reads the mid-band to decide what a human
should look at.

**A case with no graders is not a case that passes.** It returns `auto_passed =
None`, which `supervision.py` treats as "only a person can settle this". The
alternative — vacuous truth — would make an empty suite score 100%, which is the
single most misleading number an eval system can produce.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from django.conf import settings

logger = logging.getLogger(__name__)

#: How long a judge is allowed to answer for. It returns a verdict and a
#: sentence, not an essay.
JUDGE_MAX_TOKENS = 512
#: Characters of the answer shown to the judge. A judge reading 60k characters
#: costs more than the run it is judging.
JUDGE_ANSWER_CHARS = 12_000
DEFAULT_JUDGE_THRESHOLD = 0.7


class GraderError(ValueError):
    """A grader spec names something this system cannot run."""


@dataclass(frozen=True, slots=True)
class GradeContext:
    """Everything a grader is allowed to look at.

    Deliberately a flat snapshot rather than the live `AgentRun`: a grader must
    not be able to start work, spend money, or mutate the run it is judging.
    """

    answer: str = ''
    structured: dict[str, Any] | None = None
    contract_error: str = ''
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    duration_ms: int = 0
    error: str = ''
    #: The case's prose description of a good answer.
    reference: str = ''
    goal: str = ''
    #: Whose credentials the `llm_judge` grader calls the provider with.
    user_id: int | None = None

    @property
    def tools_used(self) -> set[str]:
        names = set()
        for call in self.tool_trace or []:
            name = call.get('tool') or call.get('name')
            if name:
                names.add(str(name))
        return names


@dataclass(frozen=True, slots=True)
class Grade:
    """One grader's verdict."""

    type: str
    passed: bool
    score: float
    weight: float = 1.0
    detail: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {
            'type': self.type,
            'passed': self.passed,
            'score': round(self.score, 4),
            'weight': self.weight,
            'detail': self.detail,
        }


@dataclass(frozen=True, slots=True)
class Grader:
    name: str
    #: Parameters the spec may carry, beyond `type` and `weight`.
    params: tuple[str, ...]
    #: Parameters without which the grader means nothing.
    required: tuple[str, ...]
    fn: Callable[[dict[str, Any], GradeContext], Awaitable[Grade] | Grade]
    #: True for graders that call a provider. Surfaced so a caller can price a
    #: suite before running it, and so tests can assert no network is needed.
    calls_model: bool = False
    description: str = ''


REGISTRY: dict[str, Grader] = {}


def grader(name: str, *, params: tuple[str, ...] = (),
           required: tuple[str, ...] = (), calls_model: bool = False,
           description: str = ''):
    """Declare a grader. The declaration is the schema; see the module docstring."""

    def wrap(fn):
        REGISTRY[name] = Grader(
            name=name, params=params, required=required, fn=fn,
            calls_model=calls_model, description=description,
        )
        return fn

    return wrap


# ---------------------------------------------------------------- text graders

def _text(spec: dict[str, Any], ctx: GradeContext) -> tuple[str, str]:
    """The haystack and needle, with case folding applied to both or neither."""
    haystack, needle = ctx.answer or '', str(spec.get('value', ''))
    if spec.get('ignore_case', True):
        haystack, needle = haystack.lower(), needle.lower()
    return haystack, needle


def _grade(spec: dict[str, Any], name: str, ok: bool, detail: str) -> Grade:
    return Grade(
        type=name, passed=ok, score=1.0 if ok else 0.0,
        weight=float(spec.get('weight', 1.0) or 1.0), detail=detail,
    )


@grader('contains', params=('value', 'ignore_case'), required=('value',),
        description='The answer contains this substring')
def _contains(spec, ctx):
    haystack, needle = _text(spec, ctx)
    ok = needle in haystack
    return _grade(spec, 'contains', ok, '' if ok else f'missing {needle!r}')


@grader('not_contains', params=('value', 'ignore_case'), required=('value',),
        description='The answer does not contain this substring')
def _not_contains(spec, ctx):
    haystack, needle = _text(spec, ctx)
    ok = needle not in haystack
    return _grade(spec, 'not_contains', ok, '' if ok else f'found {needle!r}')


@grader('equals', params=('value', 'ignore_case', 'strip'), required=('value',),
        description='The answer is exactly this text')
def _equals(spec, ctx):
    answer, expected = ctx.answer or '', str(spec.get('value', ''))
    if spec.get('strip', True):
        answer, expected = answer.strip(), expected.strip()
    if spec.get('ignore_case', True):
        answer, expected = answer.lower(), expected.lower()
    ok = answer == expected
    return _grade(spec, 'equals', ok, '' if ok else 'answer differs from expected')


@grader('regex', params=('pattern', 'ignore_case', 'negate'), required=('pattern',),
        description='The answer matches this regular expression')
def _regex(spec, ctx):
    pattern = str(spec.get('pattern', ''))
    flags = re.IGNORECASE if spec.get('ignore_case', True) else 0
    try:
        found = re.search(pattern, ctx.answer or '', flags) is not None
    except re.error as exc:
        # A bad pattern is a broken *case*, not a failing agent. Saying so
        # keeps the two apart in the results view.
        return _grade(spec, 'regex', False, f'invalid pattern: {exc}')
    ok = (not found) if spec.get('negate') else found
    return _grade(spec, 'regex', ok, '' if ok else f'pattern {pattern!r} did not match')


@grader('min_length', params=('value',), required=('value',),
        description='The answer is at least this many characters')
def _min_length(spec, ctx):
    want = int(spec.get('value', 0) or 0)
    got = len(ctx.answer or '')
    ok = got >= want
    return _grade(spec, 'min_length', ok, '' if ok else f'{got} < {want} characters')


@grader('max_length', params=('value',), required=('value',),
        description='The answer is at most this many characters')
def _max_length(spec, ctx):
    limit = int(spec.get('value', 0) or 0)
    got = len(ctx.answer or '')
    ok = got <= limit
    return _grade(spec, 'max_length', ok, '' if ok else f'{got} > {limit} characters')


# ----------------------------------------------------------- structure graders

@grader('json_key', params=('key', 'equals'), required=('key',),
        description="A key is present in the run's structured output")
def _json_key(spec, ctx):
    key = str(spec.get('key', ''))
    payload = ctx.structured
    if not isinstance(payload, dict):
        return _grade(spec, 'json_key', False, 'run produced no structured output')
    if key not in payload:
        return _grade(spec, 'json_key', False, f'missing key {key!r}')
    if 'equals' in spec:
        ok = payload.get(key) == spec['equals']
        return _grade(spec, 'json_key', ok, '' if ok else f'{key!r} is {payload.get(key)!r}')
    return _grade(spec, 'json_key', True, '')


@grader('contract', params=(),
        description="The run satisfied the agent's declared output contract")
def _contract(spec, ctx):
    if ctx.contract_error:
        return _grade(spec, 'contract', False, ctx.contract_error)
    ok = ctx.structured is not None
    return _grade(spec, 'contract', ok, '' if ok else 'no structured output produced')


# ---------------------------------------------------------- behaviour graders

@grader('tool_used', params=('tool',), required=('tool',),
        description='The agent called this tool at least once')
def _tool_used(spec, ctx):
    tool = str(spec.get('tool', ''))
    ok = tool in ctx.tools_used
    return _grade(spec, 'tool_used', ok, '' if ok else f'{tool} was never called')


@grader('tool_not_used', params=('tool',), required=('tool',),
        description='The agent never called this tool')
def _tool_not_used(spec, ctx):
    tool = str(spec.get('tool', ''))
    ok = tool not in ctx.tools_used
    return _grade(spec, 'tool_not_used', ok, '' if ok else f'{tool} was called')


@grader('no_error', params=(), description='The run finished without an error')
def _no_error(spec, ctx):
    ok = not ctx.error
    return _grade(spec, 'no_error', ok, ctx.error[:200])


# -------------------------------------------------------------- budget graders

@grader('max_tokens', params=('value',), required=('value',),
        description='The run spent at most this many tokens')
def _max_tokens(spec, ctx):
    limit = int(spec.get('value', 0) or 0)
    ok = ctx.tokens <= limit
    return _grade(spec, 'max_tokens', ok, '' if ok else f'{ctx.tokens} > {limit} tokens')


@grader('max_duration_ms', params=('value',), required=('value',),
        description='The run finished within this many milliseconds')
def _max_duration(spec, ctx):
    limit = int(spec.get('value', 0) or 0)
    ok = ctx.duration_ms <= limit
    return _grade(spec, 'max_duration_ms', ok, '' if ok else f'{ctx.duration_ms}ms > {limit}ms')


# ------------------------------------------------------------------- the judge

def _judge_reply(text: str) -> tuple[float, str]:
    """Pull `{score, reason}` out of a model reply.

    Forgiving about fences and surrounding prose, strict about the number: a
    reply with no parseable score is a judge failure, reported as such, never a
    default of 1.0. A judge that silently passes everything when it malfunctions
    is worse than no judge.
    """
    raw = (text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z]*\s*|\s*```$', '', raw).strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        raise ValueError('judge returned no JSON object')
    payload = json.loads(match.group(0))
    score = payload.get('score')
    if not isinstance(score, (int, float)):
        raise ValueError('judge returned no numeric score')
    return max(0.0, min(1.0, float(score))), str(payload.get('reason', ''))[:300]


JUDGE_SYSTEM = (
    'You grade one answer against a rubric. Reply with JSON only: '
    '{"score": <0..1>, "reason": "<one sentence>"}. '
    'Score 1.0 only if the answer fully satisfies the rubric. Judge the answer '
    'as given; do not reward good intentions, apologies, or promises to try again.'
)


@grader('llm_judge', params=('rubric', 'threshold', 'provider', 'model'),
        calls_model=True,
        description='A model scores the answer against a rubric')
async def _llm_judge(spec, ctx):
    from llm import access as llm

    weight = float(spec.get('weight', 1.0) or 1.0)
    threshold = float(spec.get('threshold', DEFAULT_JUDGE_THRESHOLD))
    rubric = str(spec.get('rubric') or ctx.reference or '').strip()
    if not rubric:
        return Grade('llm_judge', False, 0.0, weight,
                     'no rubric: set the grader\'s rubric or the case reference')

    answer = (ctx.answer or '')[:JUDGE_ANSWER_CHARS]
    prompt = (
        f'TASK GIVEN TO THE AGENT:\n{ctx.goal}\n\n'
        f'RUBRIC FOR A GOOD ANSWER:\n{rubric}\n\n'
        f'ANSWER TO GRADE:\n{answer or "(the agent produced no answer)"}'
    )

    try:
        completion = await llm.complete(
            provider=spec.get('provider') or getattr(settings, 'EVAL_JUDGE_PROVIDER', 'openrouter'),
            model=spec.get('model') or getattr(settings, 'EVAL_JUDGE_MODEL', ''),
            prompt=prompt,
            system_message=JUDGE_SYSTEM,
            user_id=ctx.user_id or 0,
            temperature=0.0,
            max_tokens=JUDGE_MAX_TOKENS,
        )
        score, reason = _judge_reply(completion.content)
    except Exception as exc:  # provider down, no credential, unparseable reply
        logger.warning('[Eval] llm_judge failed: %s', exc)
        # Not a pass and not a silent zero either — the detail says the judge
        # was the thing that broke, so a run full of these reads as a broken
        # rubric rather than a broken agent.
        return Grade('llm_judge', False, 0.0, weight, f'judge unavailable: {exc}')

    return Grade('llm_judge', score >= threshold, score, weight,
                 reason or f'scored {score:.2f} against a threshold of {threshold:.2f}')


# ------------------------------------------------------------------ the funnel

def validate_spec(spec: Any) -> dict[str, Any]:
    """Return a normalised grader spec, or raise `GraderError`.

    Called on write, so an unrunnable grader is a 400 at the moment someone
    saves it rather than a mystery at the moment a suite sweeps.
    """
    if not isinstance(spec, dict):
        raise GraderError('each grader must be an object')
    name = spec.get('type')
    if name not in REGISTRY:
        known = ', '.join(sorted(REGISTRY))
        raise GraderError(f'unknown grader {name!r}. Known graders: {known}')
    declared = REGISTRY[name]
    for key in declared.required:
        if spec.get(key) in (None, ''):
            raise GraderError(f'grader {name!r} needs {key!r}')
    allowed = {'type', 'weight', *declared.params}
    unknown = set(spec) - allowed
    if unknown:
        raise GraderError(
            f'grader {name!r} does not take {", ".join(sorted(unknown))}'
        )
    raw_weight = spec.get('weight', 1.0)
    if raw_weight is None:
        raw_weight = 1.0
    try:
        # Not `or 1.0`: that turns an explicit 0 into 1, which is the opposite
        # of what someone writing `"weight": 0` meant, and would silently keep
        # a grader they tried to neutralise.
        weight = float(raw_weight)
    except (TypeError, ValueError):
        raise GraderError(f'grader {name!r} has a non-numeric weight')
    if weight <= 0:
        raise GraderError(f'grader {name!r} needs a positive weight')
    return {**spec, 'weight': weight}


def validate_specs(specs: Any) -> list[dict[str, Any]]:
    if specs in (None, ''):
        return []
    if not isinstance(specs, list):
        raise GraderError('graders must be a list')
    return [validate_spec(s) for s in specs]


async def grade_all(specs: list[dict[str, Any]], ctx: GradeContext):
    """Run every grader and fold the verdicts together.

    Returns `(grades, score, passed)` where `passed` is None when there was
    nothing to decide with — see the module docstring on why that is not True.
    """
    grades: list[Grade] = []
    for spec in specs or []:
        declared = REGISTRY.get(spec.get('type'))
        if declared is None:
            # Reachable only for a spec saved before a grader was removed.
            grades.append(Grade(str(spec.get('type')), False, 0.0,
                                float(spec.get('weight', 1.0) or 1.0),
                                'grader no longer exists'))
            continue
        try:
            outcome = declared.fn(spec, ctx)
            if hasattr(outcome, '__await__'):
                outcome = await outcome
        except Exception as exc:
            logger.exception('[Eval] grader %s raised', declared.name)
            outcome = Grade(declared.name, False, 0.0,
                            float(spec.get('weight', 1.0) or 1.0),
                            f'grader raised: {exc}')
        grades.append(outcome)

    if not grades:
        return [], 0.0, None

    total_weight = sum(g.weight for g in grades) or 1.0
    score = sum(g.score * g.weight for g in grades) / total_weight
    return grades, score, all(g.passed for g in grades)


def catalog() -> list[dict[str, Any]]:
    """The grader list a UI renders its picker from. One source, same as above."""
    return [
        {
            'type': g.name,
            'params': list(g.params),
            'required': list(g.required),
            'calls_model': g.calls_model,
            'description': g.description,
        }
        for g in sorted(REGISTRY.values(), key=lambda g: g.name)
    ]
