"""
The tool library: what exists, what this user has changed about it, and the one
endpoint that changes it.

Tools are code (`chat/tools/registry.py`) and are not user-created. This module
exposes *what exists*, grouped by the grant that makes each tool reachable
(`agents/agent/runtime.py:GRANT_TOOLS`), and lays the user's own overlay
(`ToolConfig`) on top of it.

Connectors vs Plugins vs Tools:
  Tool      = one callable function the model can invoke (registry Tool)
  Connector = credential/connection info (credentials.Credential)
  Plugin    = external MCP server that advertises mcp__* tools at runtime
Skills are prompt injection, not callable - intentionally absent here.

Two levels of configuration, and they answer different questions. **Grants**
(the agent builder) say what *one agent* may reach. **This page** says what the
workspace offers at all: a tool switched off here is not offered to any agent,
not offered in chat, and not dispatchable by either - which is the kill switch
a grant cannot express, because a grant is per-agent and covers a whole group.
Absent row = code defaults, so a fresh install has nothing to read here.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .settings_schema import LOCKED_TOOLS, defaults_for, settings_for


# Grant -> human presentation. Must match GRANT_TOOLS keys in
# agents/agent/runtime.py:42 and the 8 bools in AgentBuilder. `shell` is
# included so it can be documented as unserved (UNSERVED_GRANTS).
GRANT_META: dict[str, dict[str, str]] = {
    'webSearch': {
        'label': 'Web Search',
        'description': 'Search the open web - queries, research, images and video.',
        'icon': 'search',
    },
    'scrape': {
        'label': 'Web Scrape',
        'description': 'Fetch and extract content from a URL.',
        'icon': 'globe',
    },
    'rag': {
        'label': 'Knowledge',
        'description': 'Retrieve from your indexed documents and knowledge bases.',
        'icon': 'book',
    },
    'codeExecution': {
        'label': 'Code Execution',
        'description': 'Run Python in the sandboxed interpreter.',
        'icon': 'code',
    },
    'fileOps': {
        'label': 'Files',
        'description': 'Read and write your own files within the virtual filesystem.',
        'icon': 'files',
    },
    'subAgents': {
        'label': 'Delegation',
        'description': 'Find and invoke other agents as sub-tasks.',
        'icon': 'users',
    },
    'mcp': {
        'label': 'Plugins',
        'description': 'Tools from connected plugins (MCP servers) using your credentials.',
        'icon': 'plug',
    },
    'shell': {
        'label': 'Shell',
        'description': 'Host shell access - not served. Leave off.',
        'icon': 'terminal',
    },
    'system': {
        'label': 'System',
        'description': 'Always available - no grant needed, no side effects.',
        'icon': 'clock',
    },
    'chat': {
        'label': 'Chat & Memory',
        'description': 'Conversation history and session context - chat-only, not grant-gated.',
        'icon': 'message',
    },
    'vision': {
        'label': 'Vision',
        'description': 'Ask the vision witness about an image attachment.',
        'icon': 'eye',
    },
    'artifacts': {
        'label': 'Artifacts',
        'description': 'Render HTML into a sandboxed iframe.',
        'icon': 'layout',
    },
    'internal': {
        'label': 'Internal',
        'description': 'Call this platform as the user - sensitive, gated.',
        'icon': 'shield',
    },
}


# Logical grouping for library display - broader than GRANT_TOOLS so the
# catalogue is complete even for chat-only tools that are not grant-gated.
# The library shows the *intent* of each tool; the grant check still uses
# GRANT_TOOLS in AgentToolbox.
LIBRARY_GROUPS: dict[str, tuple[str, ...]] = {
    'webSearch': ('web_search', 'deep_research', 'image_search', 'video_search'),
    'scrape': ('scrape_webpage', 'read_url'),
    'rag': ('list_knowledge_bases', 'knowledge_base_search', 'keyword_search',
            'list_documents', 'read_document'),
    'codeExecution': ('execute_python',),
    'fileOps': ('list_files', 'read_file', 'write_file', 'make_directory',
                'delete_file'),
    'subAgents': ('search_agents', 'run_agent', 'get_agent_run', 'invoke_subagent'),
    'system': ('get_current_time',),
    'mcp': (),
    'shell': (),
    'chat': ('search_conversation_history', 'get_chat_message_full_text',
             'read_attachment_text', 'read_tool_output', 'recall_context'),
    'vision': ('ask_vision',),
    'artifacts': ('render_html_artifact',),
    'internal': ('call_internal_api',),
}

#: Display order. The six grant groups first, because those are the ones an
#: agent's permissions screen mirrors; everything always-on below them.
CATEGORY_ORDER = [
    'webSearch', 'scrape', 'rag', 'codeExecution', 'fileOps', 'subAgents',
    'system', 'chat', 'vision', 'artifacts', 'internal', 'mcp', 'shell',
]

#: The six that mirror a grant in the agent builder. Sent to the client so the
#: split between "granted per agent" and "always on" is decided here, next to
#: GRANT_TOOLS, rather than by a literal array in the page.
GRANT_CATEGORIES = ['webSearch', 'scrape', 'rag', 'codeExecution', 'fileOps',
                    'subAgents']


def _display_name(tool_name: str) -> str:
    # web_search -> Web Search
    return ' '.join(word.capitalize() for word in tool_name.split('_'))


def _grant_for_tool(tool_name: str) -> str | None:
    from agents.agent.runtime import ALWAYS_AVAILABLE
    if tool_name in ALWAYS_AVAILABLE:
        return 'system'
    for grant, tools in LIBRARY_GROUPS.items():
        if tool_name in tools:
            return grant
    return None  # fallback - should not happen


def _build_catalogue(user_id: int | None):
    from agents.agent.runtime import ALWAYS_AVAILABLE, UNSERVED_GRANTS
    from chat.tools.registry import all_tools
    from chat.tools import AVAILABLE_TOOLS  # noqa: F401 - import populates registry

    from .overlay import overlay

    user_overlay = overlay(user_id)

    grouped: dict[str, list] = {k: [] for k in GRANT_META}
    for t in all_tools():
        grant = _grant_for_tool(t.name) or 'system'
        grouped.setdefault(grant, []).append(t)

    categories = []
    for grant_key, meta in GRANT_META.items():
        is_unserved = grant_key in UNSERVED_GRANTS
        category_tools = []
        for t in grouped.get(grant_key, []):
            row = user_overlay.get(t.name, {})
            func = t.schema.get('function', {})
            config = dict(defaults_for(t.name))
            config.update({
                k: v for k, v in (row.get('config') or {}).items() if k in config
            })
            category_tools.append({
                'name': t.name,
                'displayName': _display_name(t.name),
                'description': func.get('description', ''),
                'effect': t.effect,
                'parallel': t.parallel,
                'sensitive': t.sensitive,
                'requires': t.requires,
                'alwaysAvailable': t.name in ALWAYS_AVAILABLE,
                'unserved': is_unserved,
                'parameters': func.get('parameters', {}),
                # -- the overlay -------------------------------------------
                # `enabled` is the effective value (absent row = on). `locked`
                # says the switch is not the user's to flip, `settings` is the
                # schema for the knobs and `config` their current values - so
                # the page needs no per-tool knowledge of its own.
                'enabled': bool(row.get('enabled', True)) and not is_unserved,
                'locked': t.name in LOCKED_TOOLS,
                'customized': t.name in user_overlay,
                'settings': [s.as_dict() for s in settings_for(t.name)],
                'config': config,
            })
        category_tools.sort(key=lambda x: x['name'])

        category = {
            'key': grant_key,
            'label': meta['label'],
            'description': meta['description'],
            'icon': meta['icon'],
            'unserved': is_unserved,
            'grantBacked': grant_key in GRANT_CATEGORIES,
            'tools': category_tools,
            'enabledCount': sum(1 for t in category_tools if t['enabled']),
        }

        if grant_key == 'mcp' and not category_tools:
            category['note'] = (
                'Plugin tools appear here at runtime once a plugin is connected '
                'and its connector is linked. Manage them in'
            )
            categories.append(category)
            continue
        if grant_key == 'shell' and not category_tools:
            category['note'] = (
                'No sandbox exists for host shell access, so the runtime refuses '
                'it even when an agent has been granted it.'
            )
            categories.append(category)
            continue
        if not category_tools and grant_key != 'system':
            continue
        categories.append(category)

    categories.sort(key=lambda c: CATEGORY_ORDER.index(c['key'])
                    if c['key'] in CATEGORY_ORDER else 99)

    total = sum(len(c['tools']) for c in categories)
    enabled = sum(c['enabledCount'] for c in categories)
    return {
        'categories': categories,
        'totalTools': total,
        'enabledTools': enabled,
        'grants': list(GRANT_META.keys()),
        'grantCategories': GRANT_CATEGORIES,
    }


def _apply_changes(user, payload) -> None:
    """Write the overlay, deleting rows that no longer say anything.

    A row equal to the defaults is deleted rather than stored, because absent
    *is* the default: keeping it would be a second representation of the same
    state, and the day a default moves the stored one would silently pin the
    old value for everyone who had ever opened the panel.
    """
    from django.db import transaction

    from .models import ToolConfig
    from .serializers import validate_payload

    changes = validate_payload(payload)

    with transaction.atomic():
        existing = {
            row.tool_name: row
            for row in ToolConfig.objects.select_for_update().filter(
                user=user, tool_name__in=list(changes)
            )
        }
        for name, body in changes.items():
            row = existing.get(name)
            defaults = defaults_for(name)
            enabled = body.get('enabled', row.enabled if row else True)
            config = dict(row.config or {}) if row else {}
            config.update(body.get('config', {}))
            # Only what differs from the default is worth storing.
            config = {k: v for k, v in config.items()
                      if k in defaults and defaults[k] != v}

            if enabled and not config:
                if row:
                    row.delete()
                continue
            if row:
                row.enabled = enabled
                row.config = config
                row.save(update_fields=['enabled', 'config', 'updated_at'])
            else:
                ToolConfig.objects.create(
                    user=user, tool_name=name, enabled=enabled, config=config,
                )


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def tool_catalogue(request):
    """
    GET   /api/tools/  - the catalogue, with this user's overlay applied.
    PATCH /api/tools/  - change one tool or many:

        {"tool_name": "video_search", "enabled": false}
        {"tools": {"web_search": {"config": {"maxResults": 8}}, ...}}

    Both shapes answer with the whole catalogue, so the client re-renders from
    the server's view of the overlay rather than from its own optimistic guess -
    a clamped value is then visible immediately instead of on the next reload.
    """
    if request.method == 'PATCH':
        _apply_changes(request.user, request.data)
    return Response(_build_catalogue(request.user.id))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def tool_usage(request):
    """
    GET /api/tools/usage/ - how many agents use each grant.

    Returns { grantKey: count } where count is the number of SubAgents
    belonging to the caller that have that grant enabled. This powers
    "Used by N agents" chips in the library without exposing agent rows.
    Counts only agent grants (GRANT_TOOLS keys + system), not chat-only.
    """
    from agents.models import SubAgent
    from agents.agent.runtime import GRANT_TOOLS

    grant_keys = [k for k in GRANT_TOOLS if k != 'system']
    counts: dict[str, int] = {k: 0 for k in GRANT_META}
    total_agents = 0
    for grants in SubAgent.objects.filter(user=request.user).values_list(
        'tool_grants', flat=True
    ):
        total_agents += 1
        if not isinstance(grants, dict):
            continue
        for key in grant_keys:
            if grants.get(key):
                counts[key] += 1
    counts['system'] = total_agents
    return Response({'usage': counts, 'totalAgents': total_agents})
