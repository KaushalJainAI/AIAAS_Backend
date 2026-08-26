"""
Keeps `HITLReminderSchedule` in step with the request it tracks.

Hooking `HITLRequest` itself rather than the call sites is deliberate: requests
are created from the agent runtime, the HITL views and the imagine agent, and a
new one would otherwise silently opt out of reminders.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from agents.models import HITLRequest

logger = logging.getLogger(__name__)


@receiver(post_save, sender=HITLRequest, dispatch_uid='notifications.hitl_reminder_schedule')
def sync_reminder_schedule(sender, instance, created, **kwargs):
    from .models import HITLReminderSchedule
    from .reminders import deliver_escalation

    if created:
        # on_commit, so a request rolled back by a failing execution never
        # produces a notification for work that did not happen.
        def _arm():
            try:
                schedule, made = HITLReminderSchedule.objects.get_or_create(
                    hitl_request=instance,
                    defaults={'user_id': instance.user_id, 'stage': 0,
                              'next_due_at': instance.created_at},
                )
                if made:
                    # Stage 0 is "the agent needs you" — sent now, not at the
                    # next sweep, which could be minutes away.
                    deliver_escalation(schedule)
            except Exception as exc:
                logger.exception("Could not arm HITL reminders for %s: %s", instance.pk, exc)

        transaction.on_commit(_arm)
        return

    # Answered, rejected, timed out or cancelled — stop nudging.
    if instance.status != 'pending':
        try:
            schedule = HITLReminderSchedule.objects.filter(hitl_request=instance).first()
            if schedule is not None:
                schedule.cancel()
        except Exception as exc:
            logger.warning("Could not cancel HITL reminders for %s: %s", instance.pk, exc)
