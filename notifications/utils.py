import logging
from .models import Notification

logger = logging.getLogger(__name__)

def create_notification(user, type, title, message, data=None):
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
        return notif
    except Exception as e:
        logger.error(f"Failed to create notification: {e}")
        return None
