"""
URL Configuration for the LLM app.

`/api/nodes/models/` is kept as an alias for the canonical `/api/llm/models/`
because BrowserOS ships its own build and cannot be redeployed in lockstep with
the frontend.
"""
from django.urls import path

from .views import AIModelListView

urlpatterns = [
    path('llm/models/', AIModelListView.as_view(), name='ai-models'),
    path('nodes/models/', AIModelListView.as_view(), name='ai-models-legacy'),
]
