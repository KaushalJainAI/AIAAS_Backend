"""
Judge calibration: how the `llm_judge` scores against known labels.

Runs `_llm_judge` over every row of the hand-written set (through
`graders.grade_all` with a one-element spec, so it is the exact production
path) and writes a `JudgeCalibration`. A judge error counts as a failed grade
(fail closed) and is reported separately from a disagreement.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


async def calibrate(rows: list[dict], *, user_id: int = 0,
                    provider: str = '', model: str = '') -> dict:
    """Grade every row, return `{n, agreement, false_pass, false_fail, details, errors}`."""
    from . import graders

    provider = provider or getattr(settings, 'EVAL_JUDGE_PROVIDER', 'openrouter')
    model = model or getattr(settings, 'EVAL_JUDGE_MODEL', '')
    details: list[dict] = []
    agree = fp = fn = errors = 0
    for row in rows:
        ctx = graders.GradeContext(
            answer=row.get('answer', ''),
            goal=row.get('goal', ''),
            reference=row.get('rubric', ''),
            tool_trace=row.get('tool_trace', []),
            user_id=user_id,
        )
        grades, _score, _passed = await graders.grade_all(
            [{'type': 'llm_judge', 'rubric': row.get('rubric', '')}],
            ctx,
        )
        grade = grades[0] if grades else None
        passed = bool(grade.passed) if grade else False
        label = bool(row.get('label'))
        detail_text = (grade.detail if grade else 'no grade') or ''
        is_error = detail_text.startswith('judge unavailable')
        if is_error:
            errors += 1
        if passed == label and not is_error:
            agree += 1
        elif passed and not label:
            fp += 1
        elif not passed and label:
            fn += 1
        details.append({
            'id': row.get('id'), 'label': label, 'kind': row.get('kind'),
            'score': grade.score if grade else 0.0, 'passed': passed,
            'reason': detail_text,
        })
    n = len(rows) or 1
    return {
        'n': len(rows),
        'agreement': agree / n,
        'false_pass_rate': fp / n,
        'false_fail_rate': fn / n,
        'errors': errors,
        'details': details,
        'provider': provider,
        'model': model,
    }


__all__ = ['calibrate']
