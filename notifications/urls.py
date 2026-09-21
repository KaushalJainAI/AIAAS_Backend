from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    HITLReminderScheduleListView,
    NotificationPreferenceView,
    NotificationViewSet,
    PushSubscriptionListView,
    subscribe_push,
    unsubscribe_push,
    vapid_public_key,
)

router = DefaultRouter()
router.register(r'', NotificationViewSet, basename='notification')

urlpatterns = [
    # Declared before the router: its '' registration matches greedily and
    # would otherwise swallow these as notification detail lookups.
    path('preferences/', NotificationPreferenceView.as_view(), name='notification-preferences'),
    path('hitl-reminders/', HITLReminderScheduleListView.as_view(), name='hitl-reminder-schedules'),
    path('push/vapid-key/', vapid_public_key, name='push-vapid-key'),
    path('push/', PushSubscriptionListView.as_view(), name='push-subscriptions'),
    path('push/subscribe/', subscribe_push, name='push-subscribe'),
    path('push/unsubscribe/', unsubscribe_push, name='push-unsubscribe'),
    path('', include(router.urls)),
]
