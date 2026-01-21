"""
Streaming URL Configuration

HTTP endpoints for SSE streaming and event history.
"""
from django.urls import path
from . import views

app_name = 'streaming'

urlpatterns = [
    # SSE stream for execution events
    path(
        'executions/<uuid:execution_id>/stream/',
        views.ExecutionStreamView.as_view(),
        name='execution-stream'
    ),
    
    # Event history for replay
    path(
        'executions/<uuid:execution_id>/events/',
        views.ExecutionEventsHistoryView.as_view(),
        name='execution-events'
    ),
    
    # Connection status
    path(
        'status/',
        views.StreamConnectionStatusView.as_view(),
        name='connection-status'
    ),
    
    # Test endpoint (DEBUG only)
    path(
        'executions/<uuid:execution_id>/test/',
        views.test_stream_event,
        name='test-event'
    ),
]
