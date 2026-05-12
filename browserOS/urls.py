from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import OSWorkspaceViewSet, OSAppWindowViewSet, OSNotificationViewSet

router = DefaultRouter()
router.register(r'workspaces', OSWorkspaceViewSet, basename='osworkspace')
router.register(r'windows', OSAppWindowViewSet, basename='osappwindow')
router.register(r'notifications', OSNotificationViewSet, basename='osnotification')

urlpatterns = [
    path('', include(router.urls)),
]
