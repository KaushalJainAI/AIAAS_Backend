"""
Agents App URL Configuration

Agent CRUD, execution, approval/steering, triggers, HITL, the builder chat
transcript and platform settings. An agent is a `SubAgent` row; runs start from
`agents/<id>/execute/`.
"""
from django.urls import path

from .views import (
    agents,
    conversations,
    hitl,
    runs,
    system,
    triggers,
)

app_name = 'orchestrator'

urlpatterns = [
    # Agents
    path('agents/', agents.agent_list, name='agent_list'),
    path('agents/<int:agent_id>/', agents.agent_detail, name='agent_detail'),
    path('agents/<int:agent_id>/execute/', runs.agent_execute, name='agent_execute'),
    path('agents/<int:agent_id>/approve/', runs.agent_approve, name='agent_approve'),
    path('agents/<int:agent_id>/reject/', runs.agent_reject, name='agent_reject'),
    path('agents/<int:agent_id>/steer/', runs.agent_steer, name='agent_steer'),

    # Triggers — how something other than the user starts a run.
    path('triggers/', triggers.trigger_list, name='trigger_list'),
    path('triggers/<int:trigger_id>/', triggers.trigger_detail, name='trigger_detail'),
    # Fire a schedule now, through the sweep's own path — the only way to find
    # out whether a schedule works without waiting for its next slot.
    path('triggers/<int:trigger_id>/run/', triggers.trigger_run_now,
         name='trigger_run_now'),
    # The one unauthenticated route. The secret in the path is the credential;
    # see agents/views/triggers.py for why it answers 404 for every refusal.
    path('hooks/<str:secret>/', triggers.webhook_receive, name='webhook_receive'),

    # HITL
    path('hitl/pending/', hitl.pending_hitl_requests, name='pending_hitl'),
    path('hitl/<str:request_id>/respond/', hitl.respond_to_hitl, name='respond_hitl'),

    # AI Chat
    path('chat/', conversations.conversation_messages, name='chat_list'),
    path('chat/<str:conversation_id>/', conversations.conversation_messages, name='chat_detail'),
    path('chat/<str:conversation_id>/messages/<int:message_id>/',
         conversations.conversation_messages, name='chat_message_detail'),

    # Settings
    path('settings/update/', system.update_orchestrator_settings,
         name='update_orchestrator_settings'),
]
