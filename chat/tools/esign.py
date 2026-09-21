"""
Documents out for e-signature, and the question of whether they came back.

`request_signature` sends a workspace file through `ESIGN_ENGINE` (Documenso
for v1) to named signers in order. It is `irreversible` and `sensitive`: the
user sees the document, the signers and the message on the approval card.
Completion arrives on the webhook (`esign/views.py`); until then
`signature_status` reads the row. A mission (P7) will be able to wait on the
completion event instead of polling.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict

from asgiref.sync import sync_to_async

from .registry import tool

from tools_config.overlay import alimit

from tools_config.settings_schema import _ESIGN_MAX_SIGNERS

logger = logging.getLogger(__name__)

#: Signers per request. A signing ceremony with forty parties is a mail merge,
#: and each address is a place to mistype someone's inbox. Now a workspace
#: knob (`request_signature.maxSigners`); the constant stays as the floor
#: under a failed overlay read.
MAX_SIGNERS = 10
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _signers_sync(args_signers, cap: int = MAX_SIGNERS) -> list[dict]:
    signers = []
    for raw in args_signers or []:
        if not isinstance(raw, dict):
            raise ValueError('Each signer must be {"name", "email"}.')
        name = str(raw.get('name') or '').strip()[:120]
        email = str(raw.get('email') or '').strip().lower()[:254]
        if not name or not _EMAIL.match(email):
            raise ValueError(f'{raw!r} is not a signer: need a name and a valid email.')
        signers.append({'name': name, 'email': email})
    if not signers:
        raise ValueError('Name at least one signer as {"name", "email"}.')
    if len(signers) > cap:
        raise ValueError(
            f'{len(signers)} signers is more than one request may take ({cap}).')
    return signers


def _resolve_sync(user_id: int, scope, path: str):
    from inference import vfs

    parent_parts, leaf = vfs._split_leaf(scope, path)
    folder = vfs._folder_at(scope, parent_parts)
    doc = vfs._document_in(scope, folder, leaf)
    if doc is None:
        raise ValueError(f'No such file: {vfs.render(scope, parent_parts + [leaf])}.')
    try:
        data = bytes(vfs.read_binary(scope, path))
    except Exception as exc:  # noqa: BLE001
        from inference.vfs import VfsError

        if isinstance(exc, VfsError):
            raise ValueError(str(exc)) from exc
        raise
    return doc, data


@tool({
    'type': 'function',
    'function': {
        'name': 'request_signature',
        'description': (
            'Send a document in your workspace out for e-signature, to named '
            'signers in order, with a message. Completion arrives on its own; '
            'check it with signature_status. For offer letters, contracts and '
            'anything needing a recorded yes.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'The document in your workspace.'},
                'signers': {
                    'type': 'array',
                    'description': f'In signing order, at most {MAX_SIGNERS}.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string'},
                            'email': {'type': 'string'},
                        },
                        'required': ['name', 'email'],
                        'additionalProperties': False,
                    },
                },
                'message': {'type': 'string', 'description': 'Note to the signers.'},
            },
            'required': ['path', 'signers'],
            'additionalProperties': False,
        },
    },
}, requires='esign', sensitive=True, effect='irreversible')
async def request_signature(args: Dict, context: Dict) -> str:
    from esign.provider import EsignError, esign_available, send

    scope = context.get('file_scope')
    user_id = context.get('user_id')
    if scope is None or not user_id:
        return json.dumps({'error': 'This agent has no file access, so it cannot read documents.'})
    path = str(args.get('path') or '').strip()
    if not path:
        return json.dumps({'error': 'Give the path of the document to send.'})
    try:
        signers = _signers_sync(
            args.get('signers'), await alimit(context, 'request_signature', 'maxSigners'))
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    if not esign_available():
        return json.dumps({'error': 'No e-signature provider is configured on this platform.'})
    try:
        doc, data = await sync_to_async(_resolve_sync)(user_id, scope, path)
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Esign] Could not read document')
        return json.dumps({'error': 'The document could not be read.'})
    try:
        request_id = await send(
            filename=doc.name, data=data, signers=signers,
            message=str(args.get('message') or '')[:2000],
        )
    except EsignError as exc:
        return json.dumps({'error': str(exc)})

    def _save():
        from esign.models import SignatureRequest

        return SignatureRequest.objects.create(
            user_id=user_id, document_id=doc.id, path=path, signers=signers,
            message=str(args.get('message') or '')[:2000], status='sent',
            provider_request_id=request_id,
        )

    try:
        row = await sync_to_async(_save)()
    except Exception:
        logger.exception('[Esign] Could not record signature request')
        return json.dumps({'error': 'The document was sent but the request could not be recorded.'})
    return json.dumps({
        'request_id': row.id, 'status': 'sent',
        'signers': signers,
        'rendered': f'Sent {doc.name} to {len(signers)} signer(s).',
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'signature_status',
        'description': (
            'Check a signature request: who has signed and whether it is '
            'done, sent, declined or expired.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'request_id': {'type': 'integer', 'description': 'The id request_signature returned.'},
            },
            'required': ['request_id'],
            'additionalProperties': False,
        },
    },
}, requires='esign', effect='read')
async def signature_status(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        request_id = int(args.get('request_id'))
    except (TypeError, ValueError):
        return json.dumps({'error': '`request_id` must be the numeric id request_signature returned.'})
    from esign.models import SignatureRequest

    row = await SignatureRequest.objects.filter(id=request_id, user_id=user_id).afirst()
    if row is None:
        return json.dumps({'error': f'No signature request {request_id} belongs to this user.'})
    return json.dumps({
        'request_id': row.id, 'status': row.status, 'path': row.path,
        'signers': row.signers,
        'completed_at': row.completed_at.isoformat() if row.completed_at else None,
    })


ESIGN_TOOLS = ('request_signature', 'signature_status')
