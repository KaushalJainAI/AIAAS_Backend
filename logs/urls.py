"""
Logs App URL Configuration
"""
from django.urls import path

from . import views

app_name = 'logs'

urlpatterns = [
    # Insights/Analytics
    path('insights/stats/', views.execution_statistics, name='execution_statistics'),
    path('insights/workflow/<int:workflow_id>/', views.workflow_metrics, name='workflow_metrics'),
    path('insights/costs/', views.cost_breakdown, name='cost_breakdown'),
    
    # Audit Trail
    path('audit/', views.audit_list, name='audit_list'),
    path('audit/export/', views.audit_export, name='audit_export'),
    
    # Execution History
    path('executions/', views.execution_list, name='execution_list'),
    path('executions/<str:execution_id>/', views.execution_detail, name='execution_detail'),
]
