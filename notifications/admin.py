from django.contrib import admin

from .models import HITLReminderSchedule, Notification, NotificationPreference


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'type', 'is_read', 'created_at')
    list_filter = ('type', 'is_read', 'created_at')
    search_fields = ('title', 'message', 'user__username', 'user__email')
    raw_id_fields = ('user',)


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'device_notifications_enabled', 'hitl_escalation_enabled',
        'hourly_reminders_enabled', 'daily_digest_enabled', 'daily_digest_time',
        'last_digest_sent_on',
    )
    list_filter = (
        'device_notifications_enabled', 'hitl_escalation_enabled',
        'hourly_reminders_enabled', 'daily_digest_enabled', 'quiet_hours_enabled',
    )
    search_fields = ('user__username', 'user__email')
    raw_id_fields = ('user',)
    # Clearing these by hand would re-open the once-per-day email cap.
    readonly_fields = ('last_digest_sent_on', 'last_hourly_sent_at', 'created_at', 'updated_at')


@admin.register(HITLReminderSchedule)
class HITLReminderScheduleAdmin(admin.ModelAdmin):
    list_display = ('hitl_request', 'user', 'stage', 'next_due_at', 'reminders_sent', 'last_sent_at')
    list_filter = ('stage',)
    search_fields = ('user__username', 'hitl_request__title')
    raw_id_fields = ('user', 'hitl_request')
    readonly_fields = ('created_at', 'updated_at')
