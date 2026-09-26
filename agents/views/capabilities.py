"""
What an agent may be granted, as data the builder can render.

With seven grants this was documentation; with every phase adding more, the
builder needs `GET /api/orchestrator/capabilities/`, derived from `GRANT_TOOLS`
+ `UNSERVED_GRANTS` + engine availability rather than hand-written. Per grant:
its tools, its scope field, whether the engine behind it is live, and a
one-line risk note. The builder greys out a grant whose engine is `none` and
says why — a switch for a capability that cannot run is worse than none.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from agents.grants import GRANT_TOOLS, UNSERVED_GRANTS

#: Grant -> the `agent_context` field naming *which* rows or hosts it may use.
#: None means the grant needs no scope: whether is the whole question.
GRANT_SCOPES: dict[str, str | None] = {
    'webSearch': None,
    'scrape': None,
    'rag': 'knowledgeBases',
    'codeExecution': None,
    'fileOps': 'fileAccess',
    'office': 'fileAccess',
    'media': 'fileAccess',
    'browser': 'browserDomains',
    'publish': None,
    'subAgents': 'delegatesTo',
    'mcp': 'connectors',
    'voice': 'fileAccess',
    'esign': 'fileAccess',
    'talk': 'recipients',
    'data': 'dataConnections',
    'api': 'apiConnections',
    'compute': 'workspaceEgress',
    'shell': 'codeProjects',
}
#: One line per grant, for the builder. What it costs or reaches, not what it
#: does — the builder already describes that.
GRANT_RISKS: dict[str, str] = {
    'webSearch': 'Reads public web pages.',
    'scrape': 'Fetches URLs the run names.',
    'rag': 'Reads the selected knowledge bases.',
    'codeExecution': 'Runs Python in the sandbox; no network, no files.',
    'fileOps': 'Reads and writes your files within the access level.',
    'office': 'Creates documents in your files.',
    'media': 'Spends money per image; needs approval.',
    'browser': 'Acts only on the listed domains; submits pause for review.',
    'publish': 'Puts pages on the internet; above link visibility asks first.',
    'subAgents': 'Runs your other agents, with their grants by proxy.',
    'mcp': 'Reaches connected accounts under your credentials.',
    'voice': 'Transcribes recordings; synthesis spends money per call.',
    'esign': 'Sends documents out for signature; completion arrives by webhook.',
    'talk': 'Messages on five channels; unattended sends need recipients.',
    'data': 'Reads databases; writes only where the owner allowed.',
    'api': 'Calls HTTP APIs; auth comes from the vault.',
    'compute': 'Runs commands on your workspace; quotas apply.',
    'shell': 'Edits code in your projects; pushes pause for review.',
}


def engine_live(grant: str) -> tuple[bool, str]:
    """Whether the machinery behind `grant` is configured, and why not.

    The one predicate for "can this grant run here": `capability_list`
    renders it and the gallery's install path refuses on it, so the Explore
    page and the builder cannot disagree about what is installable (the
    `visible_servers_sync` rule — one predicate, two readers).
    """
    if grant == 'browser':
        from browsing.engine import browser_available

        if browser_available():
            return True, ''
        return False, 'No browser is configured on this platform (BROWSER_ENGINE=none).'
    if grant == 'codeExecution':
        from django.conf import settings

        engine = getattr(settings, 'SANDBOX_ENGINE', 'inprocess')
        return True, '' if engine else f'Engine: {engine or "inprocess"}.'
    if grant == 'voice':
        from voice.stt import stt_available
        from voice.tts import tts_available

        if stt_available() or tts_available():
            return True, ''
        return False, 'No speech engine is configured (STT_ENGINE/TTS_ENGINE=none).'
    if grant == 'esign':
        from esign.provider import esign_available

        if esign_available():
            return True, ''
        return False, 'No e-signature provider is configured (ESIGN_ENGINE=none).'
    if grant == 'compute':
        from workspaces.engine import workspace_available

        if workspace_available():
            return True, ''
        return False, 'No workspace engine is configured (WORKSPACE_ENGINE=none).'
    if grant == 'shell':
        from workspaces.engine import workspace_available

        if workspace_available():
            return True, ''
        return False, 'No workspace engine is configured (WORKSPACE_ENGINE=none).'
    return True, ''


def unavailable_grants(config: dict | None) -> list[dict[str, str]]:
    """Grants `config` holds whose engine is down, as `{grant, reason}`.

    `config` is a flat `AgentConfig` (template entry or shared agent) — the
    same `tools` keys the serializer stores and the runtime enforces, so the
    install screen refuses exactly what would arrive unable to run.
    """
    out = []
    for grant, on in ((config or {}).get('tools') or {}).items():
        if not on:
            continue
        live, reason = engine_live(grant)
        if not live:
            out.append({'grant': grant, 'reason': reason})
    return out


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def capability_list(request):
    """Every grant the runtime serves, with its tools, scope and engine state."""
    grants = []
    for key in sorted(GRANT_TOOLS):
        live, reason = engine_live(key)
        grants.append({
            'key': key,
            'tools': sorted(GRANT_TOOLS[key]),
            'scope': GRANT_SCOPES.get(key),
            'engine_live': live,
            **({'engine_reason': reason} if reason else {}),
            'risk': GRANT_RISKS.get(key, ''),
            'served': key not in UNSERVED_GRANTS,
        })
    return Response({
        'grants': grants,
        'unserved': sorted(UNSERVED_GRANTS),
    })
