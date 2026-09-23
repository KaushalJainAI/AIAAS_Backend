"""
Publishing a custom tool: snapshots that travel, credentials that do not.

`tool_to_shareable` freezes a connection row into `(tool_config, auth_shape)`.
The config is everything needed to recreate the row; the auth shape says
*what kind* of credential the copy will need (`needs: {slug, field}`) without
naming the author's row or value. `install_copy` rebuilds a private row owned
by the installer through the same serializers that validate creation, so a
snapshot that has drifted from what the API accepts fails at install with the
same message creation would give — never as a half-written row.

The invariant, asserted in tests: the JSON of a share never contains
`secret_ref`, an `Authorization` value, or a password.
"""
from __future__ import annotations

import re
from typing import Any

_REF = re.compile(r'\s*([A-Za-z0-9][A-Za-z0-9_-]*)\.([A-Za-z0-9_]+)\s*')


def _needs(secret_ref: str) -> dict[str, str] | None:
    """`slug.field` → which credential type the installer must link, or None."""
    match = _REF.fullmatch(secret_ref or '')
    if not match:
        return None
    return {'slug': match.group(1), 'field': match.group(2)}


def snapshot_api(row) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(tool_config, auth_shape)` for an `ApiConnection`. Secrets stripped."""
    auth = row.auth or {}
    kind = str(auth.get('type') or 'none').lower()
    config = {
        'name': row.name,
        'base_url': row.base_url,
        'openapi_spec': row.openapi_spec or {},
        'allowed_methods': list(row.allowed_methods or []),
    }
    shape: dict[str, Any] = {'type': kind}
    if kind == 'header':
        shape['header'] = auth.get('header') or 'X-API-Key'
    if kind == 'query':
        shape['param'] = auth.get('param') or 'api_key'
    needs = _needs(str(auth.get('secret_ref') or ''))
    if needs:
        shape['needs'] = needs
    return config, shape


def snapshot_data(row) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(tool_config, auth_shape)` for a `DataConnection`. Secrets stripped."""
    config = {
        'name': row.name,
        'kind': row.kind,
        'host': row.host,
        'port': row.port,
        'database': row.database,
        'username': row.username,
        'vfs_path': row.vfs_path,
        'ssl_mode': row.ssl_mode,
        'allow_write': bool(row.allow_write),
    }
    shape: dict[str, Any] = {}
    needs = _needs(str(row.secret_ref or ''))
    if needs:
        shape['needs'] = needs
    return config, shape


def tool_to_shareable(tool_kind: str, row) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze a live row for publishing. Raises `ValueError` on unknown kind."""
    if tool_kind == 'api':
        return snapshot_api(row)
    if tool_kind == 'data':
        return snapshot_data(row)
    raise ValueError(f'Unknown tool kind {tool_kind!r}.')


def _deduped_name(model, user, base: str) -> str:
    """Installing twice is ordinary; `unique_together (user, name)` would 500."""
    name = base
    counter = 1
    while model.objects.filter(user=user, name=name).exists():
        name = f'{base} ({counter})'
        counter += 1
    return name


def install_copy(tool_kind: str, config: dict[str, Any],
                 auth_shape: dict[str, Any], user):
    """A private row owned by `user` from a frozen snapshot.

    Validated through the creation serializers, so install enforces exactly
    what creation enforces. Auth always starts empty (anonymous): the copy
    calls only what needs no credential until its owner links their own —
    credentials never travel, not even as references. Returns `(row, needs)`
    where `needs` is `{slug, field} | None`, the credential type to link.
    """
    from .serializers import ApiConnectionSerializer, DataConnectionSerializer

    if tool_kind == 'api':
        from .models import ApiConnection

        serializer = ApiConnectionSerializer(
            data={**config,
                  'name': _deduped_name(ApiConnection, user, config['name']),
                  'auth': {'type': 'none'}},
            context={'request': _request_for(user)},
        )
    elif tool_kind == 'data':
        from .models import DataConnection

        serializer = DataConnectionSerializer(
            data={**config,
                  'name': _deduped_name(DataConnection, user, config['name']),
                  'secret_ref': ''},
            context={'request': _request_for(user)},
        )
    else:
        raise ValueError(f'Unknown tool kind {tool_kind!r}.')
    serializer.is_valid(raise_exception=True)
    row = serializer.save(user=user)
    needs = auth_shape.get('needs')
    return row, (dict(needs) if isinstance(needs, dict) else None)


def _request_for(user):
    """The serializers only read `context['request'].user`; this is that."""

    class _Request:
        pass

    request = _Request()
    request.user = user
    return request
