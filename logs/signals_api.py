"""
One writer for implicit quality signals. All call sites are one line each.

Best-effort: wrapped so a failing write never fails the user's action. Do not
name this module `signals.py` — Django reserves that for model receivers.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def record_signal(user_id, kind: str, *, execution_id=None, session_id: str = '',
                  message_id=None, **detail) -> None:
    """Record one `RunSignal` row (sync). Never raises to its caller."""
    try:
        from django.contrib.auth import get_user_model

        from .models import RunSignal

        User = get_user_model()
        user = User.objects.filter(pk=user_id).first()
        if user is None:
            return
        kwargs: dict = {'user': user, 'kind': kind, 'detail': dict(detail)}
        if execution_id is not None:
            from .models import ExecutionLog

            log = None
            try:
                log = ExecutionLog.objects.filter(execution_id=execution_id).first()
                if log is None:
                    log = ExecutionLog.objects.filter(pk=execution_id).first()
            except Exception:  # noqa: BLE001
                log = None
            if log is not None:
                kwargs['execution'] = log
        if session_id:
            kwargs['chat_session_id'] = str(session_id)[:100]
        if message_id is not None:
            try:
                from chat.models import ChatMessage

                msg = ChatMessage.objects.filter(pk=message_id).first()
                if msg is not None:
                    kwargs['chat_message'] = msg
            except Exception:  # noqa: BLE001
                pass
        RunSignal.objects.create(**kwargs)
    except Exception:  # noqa: BLE001
        logger.warning('[Signals] Could not record %s', kind, exc_info=True)


async def arecord_signal(user_id, kind: str, **kwargs) -> None:
    """Async wrapper around `record_signal`."""
    from asgiref.sync import sync_to_async

    await sync_to_async(record_signal)(user_id, kind, **kwargs)


__all__ = ['record_signal', 'arecord_signal']
