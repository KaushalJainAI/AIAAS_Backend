"""
Orchestrator-wide settings: platform metadata and the caller's LLM config.
"""

import asyncio
import logging

from adrf.decorators import api_view
from asgiref.sync import sync_to_async
from logs.models import ExecutionLog
from rest_framework import status
from rest_framework.decorators import permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def update_orchestrator_settings(request):
    """
    Update the orchestrator's LLM settings (provider & model).
    Called from the Orchestrator page when user changes the model config.
    """
    llm_provider = request.data.get('llm_provider')
    llm_model = request.data.get('llm_model')
    llm_credential = request.data.get('llm_credential')

    if not llm_provider and not llm_model and not llm_credential:
        return Response({'error': 'No settings provided'}, status=status.HTTP_400_BAD_REQUEST)

    # Written straight to the caller's profile: the profile fields are the
    # persisted state, and nothing else needs to exist.
    from core.models import UserProfile

    def _save() -> UserProfile:
        profile, _ = UserProfile.objects.get_or_create(user_id=request.user.id)
        if llm_provider:
            profile.llm_provider = llm_provider
        if llm_model:
            profile.llm_model = llm_model
        if llm_credential:
            profile.llm_credential_id = int(llm_credential)
        elif llm_credential == "":
            # Explicit clear, distinct from "not supplied" above.
            profile.llm_credential_id = None
        profile.save()
        return profile

    profile = await sync_to_async(_save)()

    return Response({
        'status': 'ok',
        'llm_type': profile.llm_provider,
        'llm_model': profile.llm_model,
        'credential_id': profile.llm_credential_id,
    })
