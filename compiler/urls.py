"""
URL Configuration for Compiler App

Workflow compilation endpoints.
"""
from django.urls import path

from .views import CompileWorkflowView, ValidateWorkflowView


urlpatterns = [
    path('workflows/<int:workflow_id>/compile/', CompileWorkflowView.as_view(), name='workflow-compile'),
    path('workflows/<int:workflow_id>/validate/', ValidateWorkflowView.as_view(), name='workflow-validate'),
]
