"""
The platform-tax control: the one exception to "one door".

`runner.sweep(..., mode='bare')` calls `llm.access.complete` once with the same
provider/model as the agent — no tools, no workspace — and grades with the same
graders. Only reachable for `group == 'external'` suites (enforced in the
command). Documented in `EVALUATION.md`.
"""
from __future__ import annotations

import logging
import time

from asgiref.sync import sync_to_async
from django.utils import timezone

from workflow_backend.thresholds import EVAL_RESULT_ANSWER_CHAR_LIMIT

from . import graders

logger = logging.getLogger(__name__)

BARE_SYSTEM = "Answer the question. End with a line 'FINAL ANSWER: <answer>'."


async def _run_bare_case(run, suite, case, agent, user) -> int:
    from agents.agent.runtime import resolve_agent_model
    from llm import access as llm

    from .runner import _open_result, _save_result

    result = await _open_result(run, case)
    started = time.monotonic()
    try:
        provider, model = await resolve_agent_model(agent, user)
        completion = await llm.complete(
            provider=provider, model=model,
            prompt=(case.goal or '').strip(),
            system_message=BARE_SYSTEM,
            user_id=user.id, temperature=0.0,
        )
        answer = completion.content or ''
        tokens = int(getattr(completion, 'tokens', 0) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.exception('[Eval] bare case %s failed', case.pk)
        await _save_result(result, suite, status='error',
                           error_message=str(exc)[:2000],
                           duration_ms=int((time.monotonic() - started) * 1000))
        return 0
    ctx = graders.GradeContext(
        answer=answer, tool_trace=[], files={}, tokens=tokens,
        duration_ms=int((time.monotonic() - started) * 1000),
        reference=case.reference or '', goal=case.goal or '', user_id=user.id,
    )
    grades, score, passed = await graders.grade_all(case.graders or [], ctx)
    truncated = len(answer) > EVAL_RESULT_ANSWER_CHAR_LIMIT
    await _save_result(
        result, suite, status='graded', execution=None,
        answer=answer[:EVAL_RESULT_ANSWER_CHAR_LIMIT],
        answer_truncated=truncated, auto_passed=passed, auto_score=score,
        grades=[g.as_dict() for g in grades], tokens=tokens,
        duration_ms=int((time.monotonic() - started) * 1000), error_message='',
    )
    return tokens


async def sweep_bare(run, suite, cases, agent, user) -> None:
    """Run every case as a bare model call, then settle the run."""
    from . import supervision
    from .runner import _finish, _reload

    tokens = 0
    for case in cases:
        tokens += await _run_bare_case(run, suite, case, agent, user)
    await _finish(run, tokens=tokens)
    run_obj = await _reload(run)
    # Mark the run's notes so the report can say no ExecutionLog was written.
    await _mark_bare(run_obj)
    await sync_to_async(supervision.notify_reviewer)(await _reload(run))


@sync_to_async
def _mark_bare(run):
    if 'no ExecutionLog' not in (run.notes or ''):
        run.notes = ((run.notes or '') + ' [bare: no ExecutionLog written]').strip()
        run.save(update_fields=['notes', 'updated_at'])


__all__ = ['sweep_bare']
