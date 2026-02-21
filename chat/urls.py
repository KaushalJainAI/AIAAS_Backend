from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ChatSessionViewSet, send_message

router = DefaultRouter()
router.register(r'sessions', ChatSessionViewSet, basename='chat-session')

urlpatterns = [
    path('', include(router.urls)),
    path('sessions/<str:session_id>/message/', send_message, name='send_message'),
]
