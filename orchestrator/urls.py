"""
Orchestrator App URL Configuration
"""
from django.urls import path

from . import views

app_name = 'orchestrator'

urlpatterns = [
    # Workflow CRUD
    path('workflows/', views.workflow_list, name='workflow_list'),
    path('workflows/<int:workflow_id>/', views.workflow_detail, name='workflow_detail'),
    
    # Version History
    path('workflows/<int:workflow_id>/versions/', views.workflow_versions, name='workflow_versions'),
    path('workflows/<int:workflow_id>/versions/<int:version_id>/restore/', views.restore_version, name='restore_version'),
    
    # Execution Control
    path('workflows/<int:workflow_id>/execute/', views.execute_workflow, name='execute_workflow'),
    path('executions/<str:execution_id>/status/', views.execution_status, name='execution_status'),
    path('executions/<str:execution_id>/pause/', views.pause_execution, name='pause_execution'),
    path('executions/<str:execution_id>/resume/', views.resume_execution, name='resume_execution'),
    path('executions/<str:execution_id>/stop/', views.stop_execution, name='stop_execution'),
    
    # HITL
    path('hitl/pending/', views.pending_hitl_requests, name='pending_hitl'),
    path('hitl/<str:request_id>/respond/', views.respond_to_hitl, name='respond_hitl'),
    
    # AI Chat
    path('chat/', views.conversation_messages, name='chat_list'),
    path('chat/<str:conversation_id>/', views.conversation_messages, name='chat_detail'),
    path('chat/context-aware/', views.context_aware_chat, name='context_aware_chat'),
    
    # AI Workflow Generation
    path('ai/generate/', views.generate_workflow, name='generate_workflow'),
    path('workflows/<int:workflow_id>/ai/modify/', views.modify_workflow, name='modify_workflow'),
    path('workflows/<int:workflow_id>/ai/suggest/', views.suggest_improvements, name='suggest_improvements'),
    
    # Thought History
    path('executions/<str:execution_id>/thoughts/', views.thought_history, name='thought_history'),
]

