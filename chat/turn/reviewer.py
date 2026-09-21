"""
The action reviewer: what makes `auto` smarter than "ask about everything".

`auto` asks for every irreversible call today. Kept as the floor, plus a
reviewer that may let an irreversible call through when it clearly matches what
the user asked for. It may only ever downgrade ask → allow, and never for the
calls listed in `NEVER_ALLOW` — deletes outside the recycle bin, sends to
unseen recipients, publishing above `link`, first-seen browser submit domains,
spend over the per-call threshold, or arguments carrying instruction-shaped
tool-result text.

A cheap model (`effort="none"`) sees the user's latest instructions, the
current todo list and the call as rendered by `describe_call` — never raw
secrets. Every decision is returned as audit (`mode`, `verdict`, `reason`) so
it can ride the trace entry and the step row. If the reviewer is unavailable
or slow (>3 s), the call asks: a failure is never an allow.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Seconds the reviewer may take before the call asks by default.
REVIEW_TIMEOUT_S = 3.0

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
    tainted: bool = False,
    seen_hosts: Any = (),
) -> tuple[bool, str] | None:
    """The no-model half: returns (allow, reason) or None to ask the judge.

    Anything this refuses is refused without spending a model call. Anything
    it does not decide goes to the judge — returning None is "ask the model",
    never "allow".
    """
    if tainted:
        return False, 'The call carries text shaped like instructions from a tool result.'
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
        f'User asked: {user_text or "(nothing captured)"}',
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
    import llm

    provider_override = getattr(__import__('django.conf', fromlist=['settings']).settings,
                                'AUTO_REVIEWER_PROVIDER', '') or provider
    model_override = getattr(__import__('django.conf', fromlist=['settings']).settings,
                             'AUTO_REVIEWER_MODEL', '') or model
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
        timeout=REVIEW_TIMEOUT_S,
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
    tainted: bool = False,
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
        try:
            outcome = await asyncio.wait_for(
                call_judge(
                    described=described, user_text=user_text, todos=todos,
                    user_id=user_id if user_id is not None
                    else (context or {}).get('user_id'),
                    provider=provider, model=model,
                ),
                timeout=REVIEW_TIMEOUT_S + 1.0,
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            logger.warning('[Reviewer] Judge failed for %s; asking.', tool_name)
            return {'allow': False, 'reason': 'The reviewer was unavailable.', 'reviewed_by': 'model'}
        if isinstance(outcome, dict):
            allow = bool(outcome.get('allow'))
            reason = str(outcome.get('reason') or '')
        else:
            allow, reason = bool(outcome[0]), str(outcome[1] or '')
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
        if name in ask_names or await permissions.default_policy(name, args, context):
            from chat.tools.describe import describe_call

            try:
                described = describe_call(name, args).get('sentence', name)
            except Exception:  # noqa: BLE001
                described = name
            verdict = await review(
                tool_name=name, args=args, described=described,
                user_text=user_text, todos=todos, context=context,
                provider=(context.get('provider', '') if isinstance(context, dict) else '') or provider,
                model=model,
                user_id=context.get('user_id'),
            )
            audits[_key(name, args)] = {
                'mode': 'auto', 'verdict': 'allow' if verdict['allow'] else 'ask',
                'reason': verdict['reason'], 'reviewed_by': verdict['reviewed_by'],
            }
            return not verdict['allow']
        return False

    policy.audits = audits  # type: ignore[attr-defined]
    return policy


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
