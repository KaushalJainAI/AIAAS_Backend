from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ChatSessionViewSet, send_message, send_message_stream, upload_file, run_workflow_from_chat, delete_message, execute_tool_view

router = DefaultRouter()
router.register(r'sessions', ChatSessionViewSet, basename='chat-session')

urlpatterns = [
    path('', include(router.urls)),
    path('execute-tool/', execute_tool_view, name='execute_tool'),
    path('sessions/<str:session_id>/message/', send_message, name='send_message'),
    path('sessions/<str:session_id>/message/stream/', send_message_stream, name='send_message_stream'),
    path('sessions/<str:session_id>/messages/<int:message_id>/', delete_message, name='delete_message'),
    path('sessions/<str:session_id>/upload/', upload_file, name='upload_file'),
    path('sessions/<str:session_id>/run-workflow/', run_workflow_from_chat, name='run_workflow_from_chat'),
]
