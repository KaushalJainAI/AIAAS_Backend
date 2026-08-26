from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    HITLReminderScheduleListView,
    NotificationPreferenceView,
    NotificationViewSet,
)

router = DefaultRouter()
router.register(r'', NotificationViewSet, basename='notification')

urlpatterns = [
    # Declared before the router: its '' registration matches greedily and
    # would otherwise swallow these as notification detail lookups.
    path('preferences/', NotificationPreferenceView.as_view(), name='notification-preferences'),
    path('hitl-reminders/', HITLReminderScheduleListView.as_view(), name='hitl-reminder-schedules'),
    path('', include(router.urls)),
]
