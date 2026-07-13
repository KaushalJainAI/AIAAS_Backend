import json
import logging
import os
import re
from functools import lru_cache
from typing import List, Dict, Any

import httpx
from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from adrf.decorators import api_view as async_api_view
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from credentials.manager import get_credential_manager
from nodes.handlers.registry import get_registry

logger = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


async def _nvidia_complete(api_key: str, system_prompt: str, user_message: str) -> str:
    """Single, direct NVIDIA NIM chat completion (no litellm, no tool-calling).

    The canvas copilot used litellm function-calling, which is unreliably slow on
    the NVIDIA free tier (multi-minute / timeouts). A plain completion on the
    nemotron model returns in ~1-3s, so we prompt for JSON actions and parse them.
    """
    model = settings.CANVAS_AGENT_MODEL.removeprefix('nvidia_nim/')
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{NVIDIA_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.2,
                "max_tokens": 1200,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"NVIDIA API error {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"].get("content") or ""


def _extract_json_object(raw: str) -> dict:
    """Pull the first balanced JSON object out of an LLM response (tolerant)."""
    if not raw:
        return {}
    s = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s).rstrip("`").strip()
    start = s.find("{")
    if start == -1:
        return {}
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    break
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}

@lru_cache(maxsize=1)
def get_cached_node_schemas():
    return get_registry().get_all_schemas()

async def _get_user_llm_credentials(user):
    """Resolve the user's stored API key for the configured CANVAS_AGENT_MODEL provider."""
    model = settings.CANVAS_AGENT_MODEL
    provider = model.split('/')[0].lower()

    slug_map = {
        'openai': 'openai',
        'google': 'gemini-api',
        'gemini': 'gemini-api',
        'anthropic': 'anthropic',
        'xai': 'xai-api',
        'perplexity': 'perplexity-api',
        'nvidia_nim': 'nvidia',
        'nvidia': 'nvidia',
    }
    slug = slug_map.get(provider)

    if slug:
        from credentials.models import Credential

        def get_cred():
            return Credential.objects.filter(
                user=user,
                credential_type__slug=slug,
                is_active=True,
                is_verified=True,
            ).first()

        active_cred = await sync_to_async(get_cred)()
        if active_cred:
            cred_data = await get_credential_manager().get_credential(active_cred.id, user.id)
            if cred_data and 'api_key' in cred_data:
                return {'api_key': cred_data['api_key']}

    # Platform default: fall back to a server-managed key from the environment so
    # the canvas copilot works out of the box (users can override with BYOK).
    platform_env = {
        'nvidia_nim': 'NVIDIA_API_KEY', 'nvidia': 'NVIDIA_API_KEY',
        'openai': 'OPENAI_API_KEY', 'gemini': 'GEMINI_API_KEY', 'google': 'GEMINI_API_KEY',
        'perplexity': 'PERPLEXITY_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY',
    }
    key = os.environ.get(platform_env.get(provider, ''), '').strip()
    if key:
        return {'api_key': key}
    return None


async def _send_canvas_actions(user_id: int, actions: List[Dict[str, Any]]) -> None:
    """Push action batch to the frontend via WebSocket."""
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    await channel_layer.group_send(
        f"canvas_agent_{user_id}",
        {
            "type": "dispatch_actions",
            "actions": actions,
        },
    )

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_node_types(request):
    schemas = get_cached_node_schemas()
    return Response({"node_types": schemas})

@async_api_view(['POST'])
@permission_classes([IsAuthenticated])
async def process_command(request):
    """
    Async endpoint to process natural language commands for the Platform Copilot.
    """
    instruction = request.data.get('instruction')
    canvas_state = request.data.get('canvas_state')
    current_url = request.data.get('current_url', 'Unknown')
    
    if not instruction:
        return Response({"error": "Instruction is required"}, status=status.HTTP_400_BAD_REQUEST)
    
    # Fallback to cached state if not provided in request
    if not canvas_state:
        canvas_state = cache.get(f"canvas_state_{request.user.id}")

    try:
        creds = await _get_user_llm_credentials(request.user)
        api_key = (creds or {}).get('api_key')
        if not api_key:
            return Response(
                {"status": "error", "message": "No LLM API key is configured for the canvas copilot."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Compact catalog of node type ids (keep the prompt small → fast response).
        node_catalog = "\n".join(
            f"- {s.get('nodeType')}: {s.get('displayName')} ({s.get('category')}) — {s.get('description', '')}"
            for s in get_cached_node_schemas()
        )
        canvas_json = json.dumps(canvas_state, indent=2) if canvas_state else "(empty canvas)"

        system_prompt = f"""/no_think
You are the Platform Copilot for a visual workflow-automation tool. Translate the
user's request into a list of canvas/UI actions.

User is at URL: {current_url}
Current canvas (nodes + edges):
{canvas_json}

Available node types — use the exact nodeType id, never invent one:
{node_catalog}

Respond with ONLY a single JSON object — no prose, no markdown fences. Schema:
{{"message": "<one short sentence for the user>", "actions": [<action>, ...]}}

Each <action> is exactly one of:
- {{"action_type": "add_node", "payload": {{"id": "<unique-id-you-choose>", "node_type": "<id>", "label": "<name>", "position": {{"x": <100-800>, "y": <100-600>}}, "config": {{}}}}}}
- {{"action_type": "connect_nodes", "payload": {{"source_id": "<nodeId>", "target_id": "<nodeId>"}}}}
- {{"action_type": "update_node", "payload": {{"node_id": "<id>", "data": {{}}}}}}
- {{"action_type": "remove_node", "payload": {{"node_id": "<id>"}}}}
- {{"action_type": "navigate", "payload": {{"path": "<route>"}}}}
- {{"action_type": "show_toast", "payload": {{"type": "success|error|info", "message": "<text>"}}}}

Rules:
- Only use nodeType ids from the list above.
- Place new nodes without overlapping existing ones (x 100-800, y 100-600).
- Give every add_node its own "id". To connect nodes you just added, reuse those
  same ids as source_id/target_id. To connect existing nodes, use their ids from
  the canvas state above.
- If no canvas change is needed, return "actions": [] and explain in "message".
"""

        raw = await _nvidia_complete(api_key, system_prompt, instruction)
        data = _extract_json_object(raw)

        actions = [
            a for a in (data.get("actions") or [])
            if isinstance(a, dict) and a.get("action_type") and isinstance(a.get("payload"), dict)
        ]
        message = data.get("message") or (
            f"Applied {len(actions)} action(s)." if actions else "No changes were needed."
        )

        if actions:
            await _send_canvas_actions(request.user.id, actions)

        return Response({
            "status": "success",
            "actions_applied": len(actions),
            "actions": actions,
            "message": message,
            "iterations": 1,
        })

    except Exception as e:
        logger.exception(f"Error in process_command: {e}")
        return Response({
            "status": "error",
            "message": str(e),
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
