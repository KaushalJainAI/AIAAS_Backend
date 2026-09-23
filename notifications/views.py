from rest_framework import generics, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import (
    HITLReminderSchedule, Notification, NotificationPreference,
    PushSubscription, ScheduledNotification,
)
from .serializers import (
    HITLReminderScheduleSerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
    PushSubscriptionSerializer,
    ScheduledNotificationSerializer,
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


class ScheduledNotificationListView(generics.ListAPIView):
    """The caller's live reminders. Creation stays in chat (the tool quotes
    the user's own timing); this is the management surface for the UI."""

    serializer_class = ScheduledNotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            ScheduledNotification.objects
            .filter(user=self.request.user, active=True,
                    next_run_at__isnull=False)
            .order_by('next_run_at')
            [:ScheduledNotification.MAX_ACTIVE_PER_USER]
        )


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def cancel_scheduled_notification(request, pk: int):
    """Cancel one live reminder. Idempotent 204; a foreign id is 404, never
    a 403 — cancelling must not oracle other users' reminders."""
    row = (ScheduledNotification.objects
           .filter(id=pk, user=request.user,
                   active=True, next_run_at__isnull=False)
           .first())
    if row is None:
        return Response({'error': 'No live reminder with that id.'},
                        status=status.HTTP_404_NOT_FOUND)
    row.cancel()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def vapid_public_key(request):
    """The VAPID public key the browser subscribes with, plus whether push is live."""
    from .webpush import is_configured, public_key

    return Response({'public_key': public_key(), 'enabled': is_configured()})


class PushSubscriptionListView(generics.ListAPIView):
    """The caller's browsers currently subscribed for closed-browser push."""

    serializer_class = PushSubscriptionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return PushSubscription.objects.filter(user=self.request.user)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def subscribe_push(request):
    """
    Store (or refresh) one browser subscription. Upsert on `endpoint`: the push
    service re-issues keys, and a second row for the same browser would push
    everything twice.
    """
    serializer = PushSubscriptionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    sub, _ = PushSubscription.objects.update_or_create(
        endpoint=serializer.validated_data['endpoint'],
        defaults={
            'user': request.user,
            'p256dh': serializer.validated_data['p256dh'],
            'auth': serializer.validated_data['auth'],
            'user_agent': serializer.validated_data.get('user_agent', '')[:255],
        },
    )
    # An endpoint belongs to one browser, hence one user: a re-login on a
    # shared machine must not leave pushes going to the previous account.
    if sub.user_id != request.user.id:
        sub.user = request.user
        sub.save(update_fields=['user', 'updated_at'])
    return Response(PushSubscriptionSerializer(sub).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def unsubscribe_push(request):
    """Remove one browser subscription. Unknown endpoints are still a 200."""
    endpoint = (request.data or {}).get('endpoint', '')
    if endpoint:
        PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return Response({'status': 'unsubscribed'})
