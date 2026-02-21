"""
URL Configuration for Nodes App

Node registry and schema endpoints.
"""
from django.urls import path

from .views import (
    NodeSchemaListView,
    NodeSchemaByCategory,
    NodeSchemaDetailView,
    AIModelListView,
)


urlpatterns = [
    path('nodes/', NodeSchemaListView.as_view(), name='node-list'),
    path('nodes/categories/', NodeSchemaByCategory.as_view(), name='node-categories'),
    path('nodes/models/', AIModelListView.as_view(), name='ai-models'),
    path('nodes/<str:node_type>/', NodeSchemaDetailView.as_view(), name='node-detail'),
]
