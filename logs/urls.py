"""
Logs app URL configuration — what an agent did, and what it was configured as.

The audit-trail and narrative routes were removed 2026-08-19: they read DAG-era
tables that no longer had a writer.
"""
from django.urls import path

from . import views

app_name = 'logs'

urlpatterns = [
    # Insights / analytics
    path('insights/stats/', views.execution_statistics, name='execution_statistics'),
    path('insights/workflow/<int:workflow_id>/', views.workflow_metrics, name='workflow_metrics'),
    path('insights/costs/', views.cost_breakdown, name='cost_breakdown'),

    # Execution history
    path('executions/', views.execution_list, name='execution_list'),
    path('executions/<str:execution_id>/', views.execution_detail, name='execution_detail'),

    # Configuration history — what the agent was when a run behaved that way.
    path('agents/<int:agent_id>/revisions/', views.revision_list, name='revision_list'),
    path('agents/<int:agent_id>/revisions/<int:number>/', views.revision_detail,
         name='revision_detail'),
]
