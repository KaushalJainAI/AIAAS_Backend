from rest_framework import serializers
from .models import HITLReminderSchedule, Notification, NotificationPreference


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
