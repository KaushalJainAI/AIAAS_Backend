from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    ImagineAgentChatView,
    ImagineAgentResumeView,
    ImagineConversationViewSet,
    ImagineViewSet,
)

router = DefaultRouter()
router.register(r'conversations', ImagineConversationViewSet, basename='imagine-conversation')
router.register(r'', ImagineViewSet, basename='imagine')

urlpatterns = [
    path('agent/chat/', ImagineAgentChatView.as_view(), name='imagine-agent-chat'),
    path('agent/resume/', ImagineAgentResumeView.as_view(), name='imagine-agent-resume'),
    path('', include(router.urls)),
]
