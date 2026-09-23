from rest_framework import serializers
from .models import (
    HITLReminderSchedule, Notification, NotificationPreference,
    PushSubscription, ScheduledNotification,
)


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'type', 'title', 'message', 'data', 'is_read', 'created_at']


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    """
    The bookkeeping fields (`last_digest_sent_on`, `last_hourly_sent_at`) are
    exposed read-only so the UI can show when the last digest went out, but the
    client must never be able to reset the once-per-day email cap.
    """

    effective_timezone = serializers.CharField(read_only=True)

    class Meta:
        model = NotificationPreference
        fields = [
            'device_notifications_enabled',
            'hitl_escalation_enabled',
            'hourly_reminders_enabled',
            'daily_digest_enabled',
            'daily_digest_time',
            'timezone',
            'effective_timezone',
            'quiet_hours_enabled',
            'quiet_hours_start',
            'quiet_hours_end',
            'last_digest_sent_on',
            'last_hourly_sent_at',
            'updated_at',
        ]
        read_only_fields = ['last_digest_sent_on', 'last_hourly_sent_at', 'updated_at']

    def validate_timezone(self, value):
        if not value:
            return value
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(value)
        except Exception:
            raise serializers.ValidationError(f"Unknown timezone: {value!r}")
        return value


class ScheduledNotificationSerializer(serializers.ModelSerializer):
    """The caller's live reminders. Read + cancel only — creation is the
    `schedule_notification` tool's job (timing is quoted from the user, and a
    second write path is a second place to forget that rule)."""

    class Meta:
        model = ScheduledNotification
        fields = ['id', 'title', 'message', 'repeat', 'send_email',
                  'next_run_at', 'last_sent_at', 'times_sent', 'created_at']
        read_only_fields = fields


class HITLReminderScheduleSerializer(serializers.ModelSerializer):
    request_title = serializers.CharField(source='hitl_request.title', read_only=True)
    request_id = serializers.CharField(source='hitl_request.request_id', read_only=True)
    request_status = serializers.CharField(source='hitl_request.status', read_only=True)
    stage_label = serializers.SerializerMethodField()

    class Meta:
        model = HITLReminderSchedule
        fields = [
            'id', 'request_id', 'request_title', 'request_status',
            'stage', 'stage_label', 'next_due_at', 'last_sent_at',
            'reminders_sent', 'created_at',
        ]

    def get_stage_label(self, obj):
        return HITLReminderSchedule.STAGE_LABELS.get(obj.stage, 'done')


class PushSubscriptionSerializer(serializers.ModelSerializer):
    """
    What the browser's `PushManager.subscribe()` returns, flattened.

    `endpoint` is the upsert key: re-subscribing the same browser refreshes its
    keys rather than duplicating the row.
    """

    p256dh = serializers.CharField(max_length=255)
    auth = serializers.CharField(max_length=255)

    class Meta:
        model = PushSubscription
        fields = ['id', 'endpoint', 'p256dh', 'auth', 'user_agent', 'created_at']
        read_only_fields = ['id', 'created_at']
        extra_kwargs = {
            'endpoint': {'validators': []},
        }
