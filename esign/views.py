"""
E-sign webhook: the provider reporting a completed (or declined) signing.

The secret in the path is the whole credential, and every refusal is the same
404 — a wrong secret, a finished request and a slug that never existed are
indistinguishable from outside, or this becomes an oracle. Completion notifies
the owner; a mission (P7) will additionally be able to wait on it.
"""
from __future__ import annotations

import json
import logging

from asgiref.sync import sync_to_async
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


def _refused():
    return JsonResponse({'detail': 'Not found.'}, status=404)


@csrf_exempt
async def signature_hook(request, secret: str):
    if request.method != 'POST':
        return _refused()
    from esign.models import SignatureRequest

    row = await SignatureRequest.objects.filter(secret=secret).afirst()
    if row is None:
        return _refused()
    try:
        body = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return _refused()
    event = str(body.get('event') or '').strip().lower()
    if row.status in ('completed', 'declined', 'expired'):
        return _refused()
    if event in ('completed', 'signed', 'fulfilled'):
        row.status = 'completed'
        row.completed_at = timezone.now()
    elif event in ('declined', 'rejected', 'cancelled'):
        row.status = 'declined'
    else:
        return _refused()

    @sync_to_async
    def _close():
        row.save(update_fields=['status', 'completed_at', 'updated_at'])
        from django.contrib.auth import get_user_model

        from notifications.utils import create_notification

        user = get_user_model().objects.filter(id=row.user_id).first()
        if user is not None:
            create_notification(
                user, 'agent_update',
                f'Signature {row.status}: {row.path.rsplit("/", 1)[-1]}',
                f'{len(row.signers or [])} signer(s).',
                data={'action_url': '/documents'}, send_email=False,
            )
            try:
                from notifications.webpush import send_web_push

                send_web_push(
                    user,
                    title=f'Signature {row.status}: {row.path.rsplit("/", 1)[-1]}',
                    body=f'{len(row.signers or [])} signer(s).',
                    action_url='/documents',
                    kind='agent_update',
                )
            except Exception:  # noqa: BLE001
                logger.exception('[Esign] Web Push failed')

    try:
        await _close()
    except Exception:  # noqa: BLE001
        logger.exception('[Esign] Failed to close signature request')
        return _refused()
    return JsonResponse({'ok': True})
