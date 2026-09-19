"""
Failure categories: why a run ended badly, in one word.

Classified from exception types first, message text last — a message match
breaks the first time the wording changes. Called at the single close point
(`agents/agent/runtime.py::_close_log`) and in `agents/recovery.py::_fail`
(`interrupted`).
"""
from __future__ import annotations


def classify(status: str = '', error_message: str = '', exc=None) -> str:
    """One of `ExecutionLog.FAILURE_CHOICES`, or '' when the run did not fail."""
    name = type(exc).__name__ if exc is not None else ''
    try:
        exc_text = str(exc) if exc is not None else ''
    except Exception:  # noqa: BLE001
        exc_text = ''
    text = f'{name} {exc_text} {error_message or ""}'.lower()

    if status in ('cancelled',):
        return 'cancelled'
    if 'interrupted' in text or 'restart' in text:
        return 'interrupted'
    if 'graphrecursion' in text or 'recursion' in text or 'step budget' in text:
        return 'step_budget'
    if 'agentturnfailed' in text or 'llm' in text or 'provider' in text or 'upstream' in text:
        return 'provider'
    if 'agentrunrefused' in text or 'guardrail' in text or 'spend cap' in text or 'unattended' in text:
        return 'guardrail'
    if 'contract' in text:
        return 'contract'
    if 'timeout' in text or status == 'timeout':
        return 'timeout'
    if 'tool' in text and 'error' in text:
        return 'tool_error'
    if status in ('failed',):
        return 'other'
    return ''
