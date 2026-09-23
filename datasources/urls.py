from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (ApiConnectionViewSet, DataConnectionViewSet,
                    shared_tool_detail, shared_tool_install,
                    shared_tool_list, tool_share)

router = DefaultRouter()
router.register(r'data-connections', DataConnectionViewSet,
                basename='data-connection')
router.register(r'api-connections', ApiConnectionViewSet,
                basename='api-connection')

urlpatterns = [
    # Sharing: publish preview / publish / withdraw one of my tools.
    path('<str:kind>/<int:tool_id>/share/', tool_share,
         name='tool-share'),
    # The shared catalogue (signed-in listing + by-slug reads + installs).
    path('shared/', shared_tool_list, name='shared-tool-list'),
    path('shared/<slug:slug>/', shared_tool_detail,
         name='shared-tool-detail'),
    path('shared/<slug:slug>/install/', shared_tool_install,
         name='shared-tool-install'),
] + router.urls
