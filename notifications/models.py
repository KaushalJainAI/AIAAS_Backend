from datetime import time as dt_time

from django.db import models
from django.conf import settings
from django.utils import timezone

class Notification(models.Model):
    NOTIFICATION_TYPES = (
        ('workflow_failed', 'Workflow Failed'),
        ('new_message', 'New Message'),
        ('hitl_request', 'Permission Required'),
        ('hitl_reminder', 'HITL Reminder'),
        ('hitl_digest', 'Daily HITL Digest'),
        ('image_ready', 'Image Generation Complete'),
        ('system', 'System Alert'),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    type = models.CharField(max_length=50, choices=NOTIFICATION_TYPES, default='system')
    title = models.CharField(max_length=255)
    message = models.TextField()
    data = models.JSONField(default=dict, blank=True, null=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.title}"


class NotificationPreference(models.Model):
    """
    Per-user delivery rules for HITL nudges.

    Three independent channels sit on top of one another and deliberately do
    not use the same transport:

    * escalation  — one unanswered request, nudged at +0, +1h, +1d then dropped
    * hourly      — optional standing nag while anything at all is pending
    * daily digest— one roll-up of everything open, at a time the user picks

    Escalation and hourly are device-only (browser/BrowserOS notification).
    Email is reserved for the digest and hard-capped at one per calendar day in
    the user's own timezone by `last_digest_sent_on` — see `reminders.py`.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_preference',
    )

    # Device notifications (browser Notifications API + BrowserOS shell)
    device_notifications_enabled = models.BooleanField(
        default=True,
        help_text='Raise an OS-level notification for HITL nudges',
    )

    # Escalation ladder
    hitl_escalation_enabled = models.BooleanField(
        default=True,
        help_text='Nudge at +0, +1h and +1d for each unanswered request',
    )

    # Optional standing reminder
    hourly_reminders_enabled = models.BooleanField(
        default=False,
        help_text='Also remind hourly while any request is still pending',
    )

    # Daily digest — the only channel permitted to send email
    daily_digest_enabled = models.BooleanField(default=True)
    daily_digest_time = models.TimeField(
        default=dt_time(9, 0),
        help_text="Local wall-clock time to send the digest, in `timezone`",
    )
    timezone = models.CharField(
        max_length=64,
        blank=True,
        help_text="IANA timezone for digest/quiet hours; blank falls back to the user's profile",
    )

    # Quiet hours suppress device pings only; the digest keeps its chosen time
    quiet_hours_enabled = models.BooleanField(default=False)
    quiet_hours_start = models.TimeField(default=dt_time(22, 0))
    quiet_hours_end = models.TimeField(default=dt_time(8, 0))

    # Bookkeeping — the once-per-day email cap and the hourly rate limit
    last_digest_sent_on = models.DateField(
        null=True,
        blank=True,
        help_text="Local date of the last digest; the hard 'one email per day' guard",
    )
    last_hourly_sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Notification Preference'
        verbose_name_plural = 'Notification Preferences'

    def __str__(self):
        return f"Notification preferences for {self.user}"

    @property
    def effective_timezone(self) -> str:
        """Own timezone, else the profile's, else UTC."""
        if self.timezone:
            return self.timezone
        profile = getattr(self.user, 'profile', None)
        return getattr(profile, 'timezone', None) or 'UTC'


class HITLReminderSchedule(models.Model):
    """
    Escalation state for one HITL request.

    `next_due_at` is the single field the sweep queries, so an exhausted or
    answered request costs nothing to skip: it is set to NULL and drops out of
    the index. Stage numbering matches `STAGE_OFFSETS` below.
    """

    # Offsets from the HITL request's creation, not from the previous send, so
    # a late sweep cannot push the whole ladder forward.
    STAGE_OFFSETS = [0, 60 * 60, 24 * 60 * 60]  # immediate, +1 hour, +1 day

    STAGE_LABELS = {
        0: 'immediate',
        1: 'one hour',
        2: 'one day',
    }

    hitl_request = models.OneToOneField(
        'orchestrator.HITLRequest',
        on_delete=models.CASCADE,
        related_name='reminder_schedule',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='hitl_reminder_schedules',
    )

    stage = models.IntegerField(
        default=0,
        help_text='Index into STAGE_OFFSETS of the next nudge to send',
    )
    next_due_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='When the next nudge is due; NULL once exhausted or answered',
    )
    last_sent_at = models.DateTimeField(null=True, blank=True)
    reminders_sent = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'HITL Reminder Schedule'
        verbose_name_plural = 'HITL Reminder Schedules'
        ordering = ['next_due_at']
        indexes = [
            models.Index(fields=['next_due_at']),
            models.Index(fields=['user', 'next_due_at']),
        ]

    def __str__(self):
        return f"Reminder stage {self.stage} for {self.hitl_request_id}"

    @property
    def is_exhausted(self) -> bool:
        return self.stage >= len(self.STAGE_OFFSETS)

    def advance(self, sent_at=None) -> None:
        """Record a delivered nudge and arm the next rung of the ladder."""
        sent_at = sent_at or timezone.now()
        self.last_sent_at = sent_at
        self.reminders_sent += 1
        self.stage += 1
        self.next_due_at = self.due_at_for_stage(self.stage)
        self.save(update_fields=['last_sent_at', 'reminders_sent', 'stage', 'next_due_at', 'updated_at'])

    def due_at_for_stage(self, stage: int):
        """Absolute due time for `stage`, or None once the ladder is spent."""
        if stage >= len(self.STAGE_OFFSETS):
            return None
        from datetime import timedelta
        base = self.hitl_request.created_at
        return base + timedelta(seconds=self.STAGE_OFFSETS[stage])

    def cancel(self) -> None:
        """Stop the ladder — the request is no longer pending."""
        if self.next_due_at is not None:
            self.next_due_at = None
            self.save(update_fields=['next_due_at', 'updated_at'])
