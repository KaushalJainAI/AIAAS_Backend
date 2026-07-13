import logging
import threading
from django.conf import settings
from django.core.mail import send_mail
from .models import Notification

logger = logging.getLogger(__name__)


def _split_env_list(value):
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def should_send_email_notification(notification):
    """Return whether this notification should also be sent by email."""
    if not getattr(settings, 'NOTIFICATIONS_EMAIL_ENABLED', False):
        return False
    if not notification.user.email:
        return False

    type_allowlist = getattr(settings, 'NOTIFICATIONS_EMAIL_TYPES', [])
    if type_allowlist and notification.type not in type_allowlist:
        return False

    data = notification.data or {}
    if data.get('email') is False or data.get('send_email') is False:
        return False

    return True


def send_notification_email(notification):
    """Send a notification email without blocking the request path."""
    subject_prefix = getattr(settings, 'NOTIFICATIONS_EMAIL_SUBJECT_PREFIX', '[AIAAS]')
    subject = f"{subject_prefix} {notification.title}".strip()
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', '') or getattr(settings, 'EMAIL_HOST_USER', '')
    message = notification.message

    def send():
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=[notification.user.email],
                fail_silently=False,
            )
        except Exception as exc:
            logger.error(
                "Failed to send notification email notification_id=%s user_id=%s: %s",
                notification.id,
                notification.user_id,
                exc,
            )

    threading.Thread(target=send, daemon=True).start()


def create_notification(user, type, title, message, data=None, send_email=None):
    if not user:
        return None
    try:
        notif = Notification.objects.create(
            user=user,
            type=type,
            title=title,
            message=message,
            data=data or {}
        )
        if send_email is not False and (send_email is True or should_send_email_notification(notif)):
            send_notification_email(notif)
        return notif
    except Exception as e:
        logger.error(f"Failed to create notification: {e}")
        return None
