"""
URL Configuration for the LLM app.

`/api/nodes/models/` is kept as an alias for the canonical `/api/llm/models/`
because BrowserOS ships its own build and cannot be redeployed in lockstep with
the frontend.
"""
from django.urls import path

from .views import AIModelListView, ModelFallbackView, ModelRefreshView

urlpatterns = [
    path('llm/models/', AIModelListView.as_view(), name='ai-models'),
    path('nodes/models/', AIModelListView.as_view(), name='ai-models-legacy'),
    path('llm/models/refresh/', ModelRefreshView.as_view(), name='ai-models-refresh'),
    path('llm/fallback/', ModelFallbackView.as_view(), name='model-fallback'),
]
