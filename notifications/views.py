from rest_framework import generics, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import HITLReminderSchedule, Notification, NotificationPreference
from .serializers import (
    HITLReminderScheduleSerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
)

class NotificationViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        notification.is_read = True
        notification.save()
        return Response({'status': 'marked as read'})

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        self.get_queryset().update(is_read=True)
        return Response({'status': 'all marked as read'})


class NotificationPreferenceView(generics.RetrieveUpdateAPIView):
    """
    GET/PATCH the caller's own reminder settings.

    get_or_create rather than 404 on first read: every user has preferences,
    they just may not have been written yet.
    """

    serializer_class = NotificationPreferenceSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        prefs, _ = NotificationPreference.objects.get_or_create(user=self.request.user)
        return prefs


class HITLReminderScheduleListView(generics.ListAPIView):
    """Read-only view of the caller's armed escalation ladders."""

    serializer_class = HITLReminderScheduleSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            HITLReminderSchedule.objects
            .select_related('hitl_request')
            .filter(user=self.request.user)
            .order_by('next_due_at')
        )
