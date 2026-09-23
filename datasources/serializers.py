"""
User-private custom tools, as the API sees them.

A custom tool is a connection row (`DataConnection` / `ApiConnection`) —
creation writes rows the generic caller tools (`call_api`, `query_sql`)
already read, so there is no new execution surface here, only validation at
write:

- hosts go through the SSRF half of the egress guard (`check_egress` with no
  scope — a host the platform must never reach is refused whatever any
  allowlist says);
- `secret_ref` names a *type* slug (`slug.field`, see `credentials/refs.py`)
  resolved per caller at dispatch, so a row can never point at another user's
  credential — but a typo'd slug would fail silently at call time, which is
  why the slug must name a real `CredentialType` here;
- raw secrets are never accepted: `auth` carries a reference or nothing, and
  an installed/shared copy carries neither (see `sharing.py`).
"""
from __future__ import annotations

import json

from rest_framework import serializers

from core.safety.net import check_egress
from credentials.models import CredentialType

from .models import ApiConnection, DataConnection

#: OpenAPI documents are routinely megabytes; the tools only ever read the
#: operation index, so the stored document is capped and the refusal says so.
OPENAPI_SPEC_MAX_BYTES = 500_000

API_AUTH_TYPES = ('none', 'bearer', 'basic', 'header', 'query')

HTTP_METHODS = ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS')

REF_HELP = 'Vault reference as "type-slug.field" — never a raw secret.'


def _validate_secret_ref(value: str) -> str:
    """`slug.field` where the slug is a real credential type.

    The slug is a *type*, resolved per caller at dispatch
    (`CredentialManager.lookup_by_slug_sync(slug, user_id)`), so cross-user
    leakage is impossible by construction — but an unknown slug would only
    fail at call time, silently, which is why this fails at write instead.
    """
    ref = (value or '').strip()
    if not ref:
        return ''
    import re as _re

    match = _re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_-]*)\.([A-Za-z0-9_]+)', ref)
    if not match:
        raise serializers.ValidationError(
            'Must look like "type-slug.field" (e.g. "my-api-token.api_key").')
    slug = match.group(1)
    if not CredentialType.objects.filter(slug=slug).exists():
        raise serializers.ValidationError(
            f'No credential type {slug!r} exists — connect it under '
            f'Credentials first, then reference it here.')
    return ref


def _check_host(host: str) -> None:
    """Refuse hosts the platform must never reach, with the guard's reason."""
    ok, reason = check_egress(host, None)
    if not ok:
        raise serializers.ValidationError(reason)


class ApiConnectionSerializer(serializers.ModelSerializer):
    """One HTTP API the user may call through `call_api`.

    `auth.type='none'` (or an empty `auth`) means anonymous — the only shape
    that needs no credential. Anything else needs a `secret_ref`; the value
    itself is never stored here.
    """

    operations_count = serializers.SerializerMethodField()

    class Meta:
        model = ApiConnection
        fields = ('id', 'name', 'base_url', 'openapi_spec', 'auth',
                  'allowed_methods', 'operations_count',
                  'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def get_operations_count(self, row) -> int:
        paths = (row.openapi_spec or {}).get('paths') or {}
        if not isinstance(paths, dict):
            return 0
        return sum(
            1 for item in paths.values() if isinstance(item, dict)
            for method in item
            if method.lower() in (
                'get', 'post', 'put', 'patch', 'delete', 'head', 'options')
        )

    def validate_name(self, value: str) -> str:
        name = (value or '').strip()
        if not name:
            raise serializers.ValidationError('A name is required.')
        user = self.context['request'].user
        qs = ApiConnection.objects.filter(user=user, name=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                'You already have an API tool with this name.')
        return name

    def validate_base_url(self, value: str) -> str:
        url = (value or '').strip()
        if not url.lower().startswith(('http://', 'https://')):
            raise serializers.ValidationError(
                'Must be an http(s) URL, e.g. https://api.example.com.')
        _check_host(url)
        return url

    def validate_openapi_spec(self, value) -> dict:
        if value in (None, '', {}):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError('Must be a JSON object.')
        size = len(json.dumps(value))
        if size > OPENAPI_SPEC_MAX_BYTES:
            raise serializers.ValidationError(
                f'Too large ({size // 1024} KB); paste the paths you need '
                f'(limit {OPENAPI_SPEC_MAX_BYTES // 1024} KB). Only the '
                f'operation index is ever read.')
        return value

    def validate_auth(self, value) -> dict:
        auth = value or {}
        if not isinstance(auth, dict):
            raise serializers.ValidationError('Must be a JSON object.')
        kind = str(auth.get('type') or 'none').lower()
        if kind not in API_AUTH_TYPES:
            raise serializers.ValidationError(
                f"Must be one of {', '.join(API_AUTH_TYPES)}.")
        cleaned: dict = {'type': kind}
        if kind == 'none':
            return cleaned
        ref = _validate_secret_ref(str(auth.get('secret_ref') or ''))
        if not ref:
            raise serializers.ValidationError(
                {'secret_ref': f'Required for {kind} auth. ' + REF_HELP})
        cleaned['secret_ref'] = ref
        if kind == 'header':
            cleaned['header'] = str(auth.get('header') or 'X-API-Key')[:80]
        if kind == 'query':
            cleaned['param'] = str(auth.get('param') or 'api_key')[:80]
        return cleaned

    def validate(self, attrs):
        # `auth` absent means anonymous — but only on create. On PATCH an
        # absent key means "leave it alone", so the default must not fire
        # there or every name edit would silently drop the credential.
        if self.instance is None and 'auth' not in attrs:
            attrs['auth'] = {'type': 'none'}
        return attrs

    def validate_allowed_methods(self, value) -> list:
        methods = value or []
        if not isinstance(methods, list):
            raise serializers.ValidationError('Must be a list of methods.')
        out = [str(m).upper() for m in methods]
        unknown = [m for m in out if m not in HTTP_METHODS]
        if unknown:
            raise serializers.ValidationError(f'Unknown methods: {unknown}.')
        return sorted(set(out))


class DataConnectionSerializer(serializers.ModelSerializer):
    """One database the user may query through `query_sql` / `execute_sql`.

    `secret_ref` is optional (some databases need no password from us —
    sqlite needs none at all); when present it is validated exactly as for
    API auth. `allow_write` offers `execute_sql` for the connection; reads
    never need it.
    """

    class Meta:
        model = DataConnection
        fields = ('id', 'kind', 'name', 'host', 'port', 'database',
                  'username', 'secret_ref', 'vfs_path', 'ssl_mode',
                  'allow_write', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate_name(self, value: str) -> str:
        name = (value or '').strip()
        if not name:
            raise serializers.ValidationError('A name is required.')
        user = self.context['request'].user
        qs = DataConnection.objects.filter(user=user, name=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                'You already have a database tool with this name.')
        return name

    def validate_secret_ref(self, value: str) -> str:
        return _validate_secret_ref(value or '')

    def validate(self, attrs):
        kind = attrs.get('kind', getattr(self.instance, 'kind', ''))
        if kind == 'sqlite':
            path = (attrs.get('vfs_path',
                              getattr(self.instance, 'vfs_path', '')) or '')
            if not path.strip().startswith('/'):
                raise serializers.ValidationError(
                    {'vfs_path': 'Must be a workspace path, e.g. '
                                 '/Chat/sales.db.'})
        else:
            host = (attrs.get('host',
                              getattr(self.instance, 'host', '')) or '').strip()
            if not host:
                raise serializers.ValidationError(
                    {'host': 'A hostname is required for this kind.'})
            _check_host(host)
        port = attrs.get('port', getattr(self.instance, 'port', None))
        if port is not None and not 1 <= int(port) <= 65535:
            raise serializers.ValidationError(
                {'port': 'Must be between 1 and 65535.'})
        return attrs
