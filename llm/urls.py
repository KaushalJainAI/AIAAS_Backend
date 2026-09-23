"""
URL Configuration for the LLM app.
"""
from django.urls import path

from .views import AIModelListView, ModelFallbackView, ModelRefreshView

urlpatterns = [
    path('llm/models/', AIModelListView.as_view(), name='ai-models'),
    path('llm/models/refresh/', ModelRefreshView.as_view(), name='ai-models-refresh'),
    path('llm/fallback/', ModelFallbackView.as_view(), name='model-fallback'),
]
