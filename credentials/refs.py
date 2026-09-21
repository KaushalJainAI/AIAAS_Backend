"""
Secret references: name a credential, never contain one.

A tool argument can carry `{"secret_ref": "<slug>.<field>"}` or embed
`{{secret:<slug>.<field>}}` inside a text argument. Resolution happens inside
dispatch, after approval — so the approval card shows the reference, never the
value — through `CredentialManager`, the one door for credentials.

The model's whole job is naming which credential it needs; the value never
passes through the transcript on the way in, and `redact` scrubs it on the way
out (a page that echoes a password back must not put it into `AgentStep.result`
or the next prompt).
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: `{{secret:slug.field}}` inside a larger string.
_REF_IN_TEXT = re.compile(r'\{\{\s*secret\s*:\s*([A-Za-z0-9][A-Za-z0-9_-]*)\.([A-Za-z0-9_]+)\s*\}\}')
#: A bare `slug.field` inside a `{"secret_ref": ...}` dict.
_REF_BARE = re.compile(r'^([A-Za-z0-9][A-Za-z0-9_-]*)\.([A-Za-z0-9_]+)$')


class SecretRefError(ValueError):
    """A reference that cannot be resolved — refused, not retried."""


def find_refs(args: Any) -> list[tuple[str, str]]:
    """Every `(slug, field)` pair named anywhere inside `args`, in order."""
    found: list[tuple[str, str]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get('secret_ref')
            if isinstance(ref, str):
                m = _REF_BARE.match(ref.strip())
                if m:
                    found.append((m.group(1), m.group(2)))
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)
        elif isinstance(value, str):
            for m in _REF_IN_TEXT.finditer(value):
                found.append((m.group(1), m.group(2)))

    walk(args)
    return found


def _decrypt_column(raw: bytes | None) -> str | None:
    if not raw:
        return None
    try:
        from cryptography.fernet import Fernet

        from django.conf import settings

        return Fernet(settings.CREDENTIAL_ENCRYPTION_KEY).decrypt(bytes(raw)).decode()
    except Exception:  # noqa: BLE001 — an undecryptable column is "no value"
        return None


def _field_value(credential, field: str) -> str | None:
    """One field from a credential row. A blob field wins over the same-named
    token column — the blob is where a hand-entered credential lives, and
    shadowing it with a leftover column is how a credential the user just typed
    in stops working."""
    try:
        data = credential.get_credential_data() or {}
    except Exception:  # noqa: BLE001
        data = {}
    if isinstance(data, dict) and data.get(field) not in (None, ''):
        return str(data[field])
    if field == 'access_token':
        return _decrypt_column(credential.access_token)
    if field == 'refresh_token':
        return _decrypt_column(credential.refresh_token)
    return None


async def aresolve_refs(
    args: Any, user_id: int, allowed_slugs: set[str] | frozenset | list | tuple,
) -> Any:
    """Replace every reference in `args` with its value, or raise `SecretRefError`.

    Only slugs in `allowed_slugs` resolve — a tool that may read the Gmail token
    must not be handed the bank token because the model named it. Resolution is
    per owner: a slug naming another user's credential refuses, since the lookup
    is filtered on `user_id`.
    """
    allowed = set(allowed_slugs or ())
    refs = find_refs(args)
    if not refs:
        return args

    from asgiref.sync import sync_to_async

    from credentials.manager import CredentialManager

    values: dict[tuple[str, str], str] = {}
    for slug, field in dict.fromkeys(refs):
        if slug not in allowed:
            raise SecretRefError(
                f'Credential {slug!r} is not available to this tool.'
            )
        credential = await sync_to_async(CredentialManager.lookup_by_slug_sync)(
            slug, user_id
        )
        if credential is None:
            raise SecretRefError(f'No credential {slug!r} is connected for this user.')
        value = await sync_to_async(_field_value)(credential, field)
        if value in (None, ''):
            raise SecretRefError(
                f'Credential {slug!r} has no field {field!r}.'
            )
        values[(slug, field)] = value

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            ref = value.get('secret_ref')
            if isinstance(ref, str) and _REF_BARE.match(ref.strip()):
                m = _REF_BARE.match(ref.strip())
                assert m is not None
                return values[(m.group(1), m.group(2))]
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, tuple):
            return tuple(walk(v) for v in value)
        if isinstance(value, str):
            def sub(m: re.Match) -> str:
                return values[(m.group(1), m.group(2))]
            return _REF_IN_TEXT.sub(sub, value)
        return value

    return walk(args)


def redact(text: str, values: list[str] | tuple[str, ...] | set[str]) -> str:
    """Scrub resolved secret values out of `text` before the model sees it."""
    out = text if isinstance(text, str) else str(text)
    for value in sorted(set(values or ()), key=len, reverse=True):
        if value and len(value) >= 4:
            out = out.replace(value, '[redacted]')
    return out
