import json
import logging
from functools import lru_cache
from typing import List, Dict, Any

from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from adrf.decorators import api_view as async_api_view
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from langchain_core.messages import HumanMessage

from credentials.manager import get_credential_manager
from nodes.handlers.registry import get_registry

logger = logging.getLogger(__name__)

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
    }
    slug = slug_map.get(provider)
    if not slug:
        return None

    from credentials.models import Credential

    def get_cred():
        return Credential.objects.filter(
            user=user,
            credential_type__slug=slug,
            is_active=True,
            is_verified=True,
        ).first()

    active_cred = await sync_to_async(get_cred)()
    if not active_cred:
        return None

    cred_data = await get_credential_manager().get_credential(active_cred.id, user.id)
    if cred_data and 'api_key' in cred_data:
        return {'api_key': cred_data['api_key']}
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
        # Get user credentials
        creds = await _get_user_llm_credentials(request.user)
        
        system_prompt = f"""You are the Platform Copilot, an intelligent assistant that controls the workflow automation platform.
You can help users navigate the platform, manage workflows, and edit the visual workflow canvas.
The user is currently at URL: {current_url}
"""

        if canvas_state:
            node_schemas = get_cached_node_schemas()
            node_types_json = json.dumps(node_schemas, indent=2)
            canvas_state_json = json.dumps(canvas_state, indent=2)
            system_prompt += f"""
--- CANVAS CONTEXT ---
The user is currently viewing a workflow canvas.
Current canvas state:
{canvas_state_json}

Available node types:
{node_types_json}

Rules for canvas actions (when using dispatch_ui_actions):
- When creating or replacing nodes, ALWAYS set "type": "generic". 
- Store the actual node type identifier in "data.nodeType".
- For add_node, place new nodes at reasonable positions (x: 100-800, y: 100-600).
  Avoid overlapping existing nodes.
- Do not invent node_type values. Only use types from the available node types list.
"""

        system_prompt += "\nAlways use the provided tools to take action. When you want to modify the UI, navigate, or edit the canvas, call dispatch_ui_actions."

        initial_state = {
            "messages": [HumanMessage(content=instruction)],
            "user": request.user,
            "creds": creds or {},
            "model": settings.CANVAS_AGENT_MODEL,
            "system_prompt": system_prompt,
            "ui_actions": [],
            "iterations": 0
        }
        
        from .graph import copilot_graph
        final_state = await copilot_graph.ainvoke(initial_state)
        
        actions = final_state.get("ui_actions", [])
        
        # Send actions to frontend
        if actions:
            await _send_canvas_actions(request.user.id, actions)

        return Response({
            "status": "success",
            "actions_applied": len(actions),
            "actions": actions,
            "message": f"Applied {len(actions)} actions." if actions else "No actions to apply.",
            "iterations": final_state.get("iterations")
        })

    except Exception as e:
        logger.exception(f"Error in process_command: {e}")
        return Response({
            "status": "error",
            "message": str(e)
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
