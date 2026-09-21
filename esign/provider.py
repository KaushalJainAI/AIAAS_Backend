"""
The e-sign provider behind `ESIGN_ENGINE`.

`none` (default) — no sending; `request_signature` is not offered. With an
engine (decision D6: Documenso for v1), `send` delivers the document and
returns the provider's request id. Completion arrives on the webhook.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class EsignError(RuntimeError):
    """Written for the model: what failed and what to do instead."""


def engine() -> str:
    return (getattr(settings, 'ESIGN_ENGINE', 'none') or 'none').strip().lower()


def esign_available() -> bool:
    return engine() not in ('', 'none')


async def send(*, filename: str, data: bytes, signers: list[dict],
               message: str = '') -> str:
    """Send `data` for signature. Returns the provider request id.

    Tests patch this function.
    """
    if not esign_available():
        raise EsignError('No e-signature provider is configured on this platform.')
    from workflow_backend.httpclient import shared_client

    endpoint = getattr(settings, 'ESIGN_REMOTE_URL', '')
    token = getattr(settings, 'ESIGN_API_TOKEN', '')
    if not endpoint:
        raise EsignError('The e-signature provider has no endpoint configured.')
    try:
        resp = await shared_client().post(
            endpoint.rstrip('/'),
            headers={'Authorization': f'Bearer {token}'} if token else None,
            files={'document': (filename, data)},
            data={'signers': json.dumps(signers), 'message': message},
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('[Esign] remote call failed: %s', exc)
        raise EsignError('The e-signature service could not be reached.') from exc
    if resp.status_code >= 400:
        raise EsignError(
            f'The e-signature service refused the request ({resp.status_code}).')
    try:
        payload = resp.json()
    except ValueError as exc:
        raise EsignError('The e-signature service returned something unreadable.') from exc
    request_id = str(payload.get('request_id') or payload.get('id') or '').strip()
    if not request_id:
        raise EsignError('The e-signature service returned no request id.')
    return request_id
