"""
Validation for a write to the overlay.

The one thing rejected loudly is an unknown tool name. Everything else about a
write is forgiving (unknown setting keys dropped, out-of-range values clamped —
see `settings_schema.clean_config`) because the caller is a UI that may be a
deploy behind the server. An unknown tool is different in kind: it writes a row
that nothing will ever read, so the user is told "off" about a tool that stays
on. That is the failure this validation exists to prevent.
"""
from __future__ import annotations

from rest_framework import serializers

from .settings_schema import LOCKED_TOOLS, clean_config


def known_tool_names() -> frozenset[str]:
    from chat.tools.registry import all_tools
    from chat.tools import AVAILABLE_TOOLS  # noqa: F401 - import populates registry

    return frozenset(t.name for t in all_tools())


class ToolConfigWriteSerializer(serializers.Serializer):
    """One entry of a bulk PATCH: `{"enabled": bool, "config": {...}}`."""

    enabled = serializers.BooleanField(required=False)
    config = serializers.DictField(required=False)

    def __init__(self, *args, tool_name: str = '', **kwargs):
        self.tool_name = tool_name
        super().__init__(*args, **kwargs)

    def validate(self, attrs):
        if self.tool_name in LOCKED_TOOLS and attrs.get('enabled') is False:
            raise serializers.ValidationError(
                f"'{self.tool_name}' cannot be switched off - the assistant is told "
                f"to call it by name when a result is too large to replay."
            )
        if 'config' in attrs:
            attrs['config'] = clean_config(self.tool_name, attrs['config'])
        return attrs


def validate_payload(payload) -> dict[str, dict]:
    """Turn a request body into `{tool_name: {enabled?, config?}}` or raise.

    Accepts both shapes the UI sends: a bulk `{"tools": {...}}` for a category
    switch, and a single `{"tool_name": "...", "enabled": ...}` for one row.
    One endpoint rather than two because they differ only in arity, and two
    endpoints is two places to forget the lock list.
    """
    if not isinstance(payload, dict):
        raise serializers.ValidationError({'detail': 'Expected an object.'})

    if 'tools' in payload:
        entries = payload.get('tools')
        if not isinstance(entries, dict):
            raise serializers.ValidationError({'tools': 'Expected an object keyed by tool name.'})
    elif 'tool_name' in payload:
        entries = {payload['tool_name']: {
            k: v for k, v in payload.items() if k in ('enabled', 'config')
        }}
    else:
        raise serializers.ValidationError(
            {'detail': "Send either 'tools' (bulk) or 'tool_name' (one tool)."}
        )

    if not entries:
        raise serializers.ValidationError({'tools': 'Nothing to change.'})

    known = known_tool_names()
    unknown = sorted(set(entries) - known)
    if unknown:
        raise serializers.ValidationError(
            {'tools': f"Not tools in this library: {', '.join(unknown)}."}
        )

    cleaned: dict[str, dict] = {}
    for name, body in entries.items():
        serializer = ToolConfigWriteSerializer(data=body or {}, tool_name=name)
        serializer.is_valid(raise_exception=True)
        cleaned[name] = serializer.validated_data
    return cleaned
