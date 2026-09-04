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


def _wants_nudges(instance) -> bool:
    """Whether the agent behind this request asked to be told when it pauses.

    `guardrails['notifyOnHitl']` — the builder's "Notify me when it stops to
    ask". It gates *delivery only*: the `HITLRequest` itself is always written,
    because that row is the approval queue. Suppressing it would take the pause
    out of the Inbox and leave the run with no way to be answered, turning a
    notification preference into "abandon this run".

    So switching it off silences the push channels (the escalation ladder and,
    with it, the hourly nudge) while the request still appears in the Inbox and
    still counts in the daily digest — the digest reads `HITLRequest` directly,
    and a roll-up that omits pending work is the one thing a roll-up must never
    do.

    Defaults to True: an agent saved before this was read, or one whose run has
    no agent behind it at all, keeps being announced. Silence is the setting a
    user has to choose.
    """
    execution = getattr(instance, 'execution', None)
    agent = getattr(execution, 'subagent', None)
    if agent is None:
        return True
    return bool((agent.guardrails or {}).get('notifyOnHitl', True))


@receiver(post_save, sender=HITLRequest, dispatch_uid='notifications.hitl_reminder_schedule')
def sync_reminder_schedule(sender, instance, created, **kwargs):
    from .models import HITLReminderSchedule
    from .reminders import deliver_escalation

    if created and not _wants_nudges(instance):
        # No schedule row at all, rather than an armed-but-skipped one: an
        # armed schedule with a "skip me" flag is a second thing to remember to
        # check at every stage of the ladder. The hourly nudge is silenced
        # separately, in `reminders.pending_for_nudges` — it works from
        # `HITLRequest`, not from this schedule.
        logger.debug(
            "HITL %s: agent has notifyOnHitl off, not arming reminders", instance.pk,
        )
        return

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
