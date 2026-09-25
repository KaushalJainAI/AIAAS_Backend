"""
The action reviewer: what makes `auto` smarter than "ask about everything".

`auto` asks for every irreversible call today. Kept as the floor, plus a
reviewer that may let an irreversible call through when it clearly matches what
the user asked for. It may only ever downgrade ask → allow, and never for the
calls listed in `NEVER_ALLOW` — deletes outside the recycle bin, sends to
unseen recipients, publishing above `link`, first-seen browser submit domains,
spend over the per-call threshold, or arguments carrying instruction-shaped
tool-result text.

A cheap model (`effort="none"`) sees the user's recent instructions, the
current todo list and the call as rendered by `describe_call` — never raw
secrets. Every decision is returned as audit (`mode`, `verdict`, `reason`) so
it can ride the trace entry and the step row. If the reviewer is unavailable
or slow (>3 s), the call asks: a failure is never an allow.

**The judge is a fixed fast model, not the chat's (2026-09-24).** Until then
`import llm; llm.complete(...)` raised `AttributeError` on every call — the
funnel is `llm.access`, the package root is empty — and `review` swallowed it
as "reviewer unavailable", so `auto` never allowed anything and behaved as
`ask` with a delay. Every test replaced `_model_judge`, which is how it passed
green. Even imported correctly, falling back to the *chat* model was wrong: the
shipped default `openrouter/free` has no `none` effort rung (so the judge
reasoned inside 200 tokens) and measured 6 s / 22 s / 1.7 s with verdicts that
disagreed, against the 3 s budget. `AUTO_REVIEWER_PROVIDER/MODEL` default to a
non-reasoning model measured at 1.3-1.5 s with stable verdicts; blank falls
back to the chat model.

**A verdict is reached once per call.** `interrupt()` re-runs `tools_node`
from the top on resume, which re-reviewed the whole batch after every approval
— paying the judge again, and able to turn a call it had just allowed into a
fresh approval card. Verdicts are cached by `(session, call_id)` in-process,
the same lifetime as the steering mailbox that resumes these runs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Seconds the reviewer may take before the call asks by default.
REVIEW_TIMEOUT_S = 3.0

#: How many verdicts the in-process cache keeps, and for how long. A verdict
#: only needs to outlive one approval round-trip on the same batch.
VERDICT_CACHE_SIZE = 2048
VERDICT_CACHE_TTL_S = 3600.0

_verdicts: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()


def _cached_verdict(key: tuple[str, str] | None) -> dict[str, Any] | None:
    if key is None:
        return None
    hit = _verdicts.get(key)
    if hit is None:
        return None
    stored_at, verdict = hit
    if time.monotonic() - stored_at > VERDICT_CACHE_TTL_S:
        _verdicts.pop(key, None)
        return None
    _verdicts.move_to_end(key)
    return verdict


def _remember_verdict(key: tuple[str, str] | None, verdict: dict[str, Any]) -> None:
    if key is None:
        return
    _verdicts[key] = (time.monotonic(), verdict)
    _verdicts.move_to_end(key)
    while len(_verdicts) > VERDICT_CACHE_SIZE:
        _verdicts.popitem(last=False)


def _setting(name: str, default: Any) -> Any:
    from django.conf import settings

    return getattr(settings, name, default)

#: Argument keys naming a recipient. A send to a value seen nowhere in the
#: user's own words is never auto-allowed.
RECIPIENT_KEYS = ('to', 'recipient', 'recipients', 'channel', 'phone', 'email')

#: Tools that spend money per call. The approval card naming the cost is the
#: point — a model that decides on four image variations has spent four times
#: what was asked for, and no reviewer can tell which variation the user wants.
SPENDY_TOOLS = frozenset({'generate_image'})


def _recipients(args: dict) -> list[str]:
    found: list[str] = []
    for key in RECIPIENT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, (list, tuple)):
            found.extend(str(v).strip() for v in value if str(v).strip())
    return found


def static_check(
    *,
    tool_name: str,
    args: dict,
    user_text: str = '',
    tainted: bool | str = False,
    seen_hosts: Any = (),
) -> tuple[bool, str] | None:
    """The no-model half: returns (allow, reason) or None to ask the judge.

    Anything this refuses is refused without spending a model call. Anything
    it does not decide goes to the judge — returning None is "ask the model",
    never "allow".

    `tainted` is the name of the tool whose result this turn read like orders
    to an AI (`tools_node` sets it from `core/safety/provenance.py`), or True.
    Once a turn has been exposed, the judge's own inputs — the user's words
    and the plan — may already be steered, so no call is waved through.
    """
    if tainted:
        source = f'A {tainted} result' if isinstance(tainted, str) else 'A tool result'
        return False, (
            f'{source} this turn contained text addressed to an AI, so auto mode '
            'asks before anything irreversible for the rest of the turn.'
        )
    if tool_name in SPENDY_TOOLS:
        return False, f'{tool_name} spends money per call; the user approves each one.'
    if tool_name == 'publish_page':
        visibility = str(args.get('visibility') or 'link').strip().lower()
        if visibility != 'link':
            return False, f'Publishing above {visibility!r} always needs a human.'
    if tool_name == 'browser_act':
        from browsing.engine import looks_submitting

        if looks_submitting(args.get('steps')):
            return False, (
                'These steps move toward submitting, paying or sending, '
                'and the whole call needs a human.'
            )
    if tool_name == 'browser_act':
        steps = args.get('steps') or []
        Touchy = ('download', 'upload')
        if isinstance(steps, list) and any(
            isinstance(s, dict) and str(s.get('action') or '').lower() in Touchy
            for s in steps
        ):
            return False, 'A browser step moving files always needs a human.'
        url = str(args.get('url') or '')
        from browsing.engine import host_of

        host = host_of(url) if url else ''
        if host and host not in set(seen_hosts or ()):
            return False, f'{host} has not been acted on before in this session.'
    for recipient in _recipients(args):
        if recipient.lower() not in (user_text or '').lower():
            return False, (
                f'Recipient {recipient!r} appears nowhere in what the user asked for.'
            )
    return None


def _judge_prompt(*, described: str, user_text: str, todos: Any) -> str:
    lines = [
        'You review one tool call the agent wants to make without asking.',
        'Answer with the first word ALLOW or ASK, then one short reason.',
        'ALLOW only when the call is plainly the user\'s request carried out,',
        'with the same target and the same content they asked for.',
        'What the user said (oldest first; the last line is the latest):',
        user_text or '(nothing captured)',
    ]
    try:
        items = list(todos or [])
    except TypeError:
        items = []
    if items:
        lines.append('Current plan:')
        for item in items[:10]:
            if isinstance(item, dict):
                lines.append(f"- [{item.get('status', '')}] {item.get('text', '')}")
            else:
                lines.append(f'- {item}')
    lines.append(f'Proposed call: {described}')
    return '\n'.join(lines)


async def _model_judge(
    *,
    described: str,
    user_text: str,
    todos: Any,
    user_id: int | None,
    provider: str,
    model: str,
) -> tuple[bool, str]:
    """Ask the cheap model. Raises on any failure — the caller turns that into ask."""
    from llm import access as llm

    # Both halves of the pair come from one place: a configured provider with
    # the chat's model (or the reverse) names a model that provider lacks.
    configured_model = _setting('AUTO_REVIEWER_MODEL', '')
    if configured_model:
        provider_override = _setting('AUTO_REVIEWER_PROVIDER', '') or provider
        model_override = configured_model
    else:
        provider_override, model_override = provider, model
    completion = await asyncio.wait_for(
        llm.complete(
            provider=provider_override,
            model=model_override,
            prompt=_judge_prompt(described=described, user_text=user_text, todos=todos),
            system_message=(
                'You are an approval reviewer. Be conservative: when in doubt, ASK.'
            ),
            user_id=user_id,
            temperature=0,
            max_tokens=200,
            effort='none',
        ),
        timeout=_setting('AUTO_REVIEWER_TIMEOUT_S', REVIEW_TIMEOUT_S),
    )
    text = (completion.content or '').strip()
    first, _, rest = text.partition(' ')
    if first.upper().startswith('ALLOW'):
        return True, rest.strip() or 'The call matches what the user asked for.'
    return False, rest.strip() or 'The reviewer was not convinced.'


async def review(
    *,
    tool_name: str,
    args: dict,
    described: str,
    user_text: str = '',
    todos: Any = (),
    context: dict | None = None,
    tainted: bool | str = False,
    seen_hosts: Any = (),
    judge: Callable[..., Any] | None = None,
    provider: str = '',
    model: str = '',
    user_id: int | None = None,
) -> dict[str, Any]:
    """Allow or ask for one irreversible call under `auto`.

    Returns `{'allow': bool, 'reason': str, 'reviewed_by': 'rules'|'model'}`.
    Never raises: every failure mode — including the judge timing out — is an
    ask, because a failure must never be an allow.
    """
    try:
        static = static_check(
            tool_name=tool_name, args=args or {}, user_text=user_text,
            tainted=tainted, seen_hosts=seen_hosts,
        )
        if static is not None:
            allow, reason = static
            return {'allow': allow, 'reason': reason, 'reviewed_by': 'rules'}
        call_judge = judge or _model_judge
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(
                call_judge(
                    described=described, user_text=user_text, todos=todos,
                    user_id=user_id if user_id is not None
                    else (context or {}).get('user_id'),
                    provider=provider, model=model,
                ),
                timeout=_setting('AUTO_REVIEWER_TIMEOUT_S', REVIEW_TIMEOUT_S) + 1.0,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            # Logged with its type: "unavailable" hid an AttributeError on
            # every call for as long as auto mode existed.
            logger.warning(
                '[Latency] reviewer %dms fail %s (%s: %s); asking.',
                (time.monotonic() - started) * 1000, tool_name,
                type(exc).__name__, exc,
            )
            return {'allow': False, 'reason': 'The reviewer was unavailable.', 'reviewed_by': 'model'}
        if isinstance(outcome, dict):
            allow = bool(outcome.get('allow'))
            reason = str(outcome.get('reason') or '')
        else:
            allow, reason = bool(outcome[0]), str(outcome[1] or '')
        logger.info('[Latency] reviewer %dms %s %s',
                    (time.monotonic() - started) * 1000,
                    'allow' if allow else 'ask', tool_name)
        return {'allow': allow, 'reason': reason or 'Reviewed.', 'reviewed_by': 'model'}
    except Exception:  # noqa: BLE001 — a failure is never an allow
        logger.exception('[Reviewer] review failed for %s', tool_name)
        return {'allow': False, 'reason': 'The reviewer failed.', 'reviewed_by': 'rules'}


def auto_policy(
    *,
    user_text: str,
    todos: Any = (),
    provider: str = '',
    model: str = '',
):
    """An approval policy for `auto` chat turns: ask, unless the reviewer allows.

    The ask floor first (sensitive names + the default credentialed-call
    policy); the reviewer may only downgrade. Returned callable matches the
    policy shape `tools_node` expects: True means pause for approval.
    """
    from chat.tools import permissions
    from chat.tools.registry import sensitive_names

    ask_names = frozenset(sensitive_names())
    # Reviewer audits by call identity, read back in pass 4 for the trace
    # entry and the step row. Keyed by tool + stable arguments rather than by
    # context: the policy never sees the call id, and two identical calls in
    # one batch would reach the same verdict anyway.
    audits: dict[str, dict[str, Any]] = {}

    def _key(name: str, args: dict) -> str:
        import json as _json

        try:
            return name + ':' + _json.dumps(args or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return name

    async def policy(name: str, args: dict, context: dict) -> bool:
        from chat.tools.agents import SUBAGENT_ANSWER_TOOL

        if name == SUBAGENT_ANSWER_TOOL:
            # The manager deciding for its worker: no judge — in `auto` the
            # orchestrator *is* the judge — but the floor still holds.
            verdict = await subagent_floor(args, context, user_text=user_text)
            audits[_key(name, args)] = {
                'mode': 'auto', 'verdict': 'ask' if verdict else 'allow',
                'reason': verdict or 'The orchestrator decided for its worker.',
                'reviewed_by': 'rules' if verdict else 'orchestrator',
            }
            return bool(verdict)
        if name in ask_names or await permissions.default_policy(name, args, context):
            # One verdict per call. `tools_node` passes the call id, and a
            # resumed node (after an approval) re-runs this for the whole
            # batch — reuse what was decided rather than asking again.
            call_id = context.get('call_id') if isinstance(context, dict) else None
            cache_key = (
                (str(context.get('session_id') or ''), str(call_id))
                if call_id else None
            )
            verdict = _cached_verdict(cache_key)
            if verdict is None:
                from chat.tools.describe import describe_call

                try:
                    described = describe_call(name, args).get('sentence', name)
                except Exception:  # noqa: BLE001
                    described = name
                # The plan as it stands at this batch, not as it stood when
                # the turn began — the model rewrites it between batches.
                live_todos = context.get('todos') if isinstance(context, dict) else None
                sink = context.get('sink') if isinstance(context, dict) else None
                if sink is not None:
                    # The judge takes a second or so; without a frame the
                    # chat looks frozen for exactly that long.
                    try:
                        from .events import Event

                        await sink(Event.STATUS, {
                            'phase': 'reviewing',
                            'message': f'Auto mode is checking: {described}',
                        })
                    except Exception:  # noqa: BLE001 — a status must not block review
                        logger.debug('[Reviewer] status frame failed', exc_info=True)
                verdict = await review(
                    tool_name=name, args=args, described=described,
                    user_text=user_text,
                    todos=live_todos if live_todos is not None else todos,
                    context=context,
                    tainted=context.get('tainted_by') or False,
                    provider=(context.get('provider', '') if isinstance(context, dict) else '') or provider,
                    model=model,
                    user_id=context.get('user_id'),
                )
                _remember_verdict(cache_key, verdict)
            audits[_key(name, args)] = {
                'mode': 'auto', 'verdict': 'allow' if verdict['allow'] else 'ask',
                'reason': verdict['reason'], 'reviewed_by': verdict['reviewed_by'],
            }
            return not verdict['allow']
        return False

    policy.audits = audits  # type: ignore[attr-defined]
    return policy


async def subagent_floor(args: dict, context: dict, *, user_text: str = '') -> str:
    """Why letting a worker act still needs the person under `auto`, or ''.

    The same no-model rules `static_check` applies to the chat's own calls,
    applied to the worker's pending call: in `auto` the manager may approve
    whatever `auto` could have run on its own, and nothing it could not — a
    spend, a publish above `link`, a recipient the user never named, a first
    browser submit, or a turn that has read instruction-shaped text.
    """
    from chat.tools.agents import _pending_row

    row = await _pending_row(str((args or {}).get('execution_id') or ''),
                             str((args or {}).get('call_id') or ''),
                             (context or {}).get('user_id'))
    if row is None or row.request_type == 'clarification':
        return ''
    if str((args or {}).get('decision') or '').strip().lower() == 'reject':
        return ''
    ctx = row.context_data or {}
    verdict = static_check(
        tool_name=str(ctx.get('tool') or ''), args=ctx.get('args') or {},
        user_text=user_text, tainted=(context or {}).get('tainted_by') or False,
    )
    if verdict is not None and verdict[0] is False:
        return f"The worker's request needs you: {verdict[1]}"
    return ''


def audit_for(policy: Any, name: str, args: dict) -> dict[str, Any] | None:
    """The reviewer audit for one call, if its policy kept one."""
    import json as _json

    audits = getattr(policy, 'audits', None)
    if not audits:
        return None
    try:
        key = name + ':' + _json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return audits.get(key)


def read_only_source(*, user_id: int | None, memory_enabled: bool = True,
                     session_key: str | None = None, file_scope: Any = None):
    """A `TurnContext.tool_source` for `plan` mode: look, don't touch.

    The offered catalogue filtered to `READ_ONLY_TOOLS` — the same set the
    agent ladder's `plan` level intersects with, so chat plan and agent plan
    withhold exactly the same tools. A gate the user can approve would be
    `review` under a better name; withholding means the model plans around
    what exists rather than asking about what does not.
    """
    async def _source():
        from chat.tools import READ_ONLY_TOOLS, get_available_tools

        offered = await get_available_tools(
            user_id, memory_enabled=memory_enabled,
            session_key=session_key, file_scope=file_scope,
        )
        names = {t['function']['name'] for t in offered} & set(READ_ONLY_TOOLS)
        return [t for t in offered if t['function']['name'] in names]

    return _source
