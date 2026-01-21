"""
Notification System for Async Approvals

Supports email and push notifications for HITL requests.
"""
import logging
from datetime import datetime
from typing import Any
from enum import Enum

logger = logging.getLogger(__name__)


class NotificationChannel(str, Enum):
    """Available notification channels."""
    EMAIL = "email"
    PUSH = "push"
    WEBSOCKET = "websocket"
    SMS = "sms"


class NotificationPriority(str, Enum):
    """Notification priority levels."""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class NotificationService:
    """
    Sends notifications through various channels.
    
    Usage:
        service = NotificationService()
        await service.send(
            user_id=1,
            channel=NotificationChannel.EMAIL,
            subject="Approval Required",
            body="Please approve the workflow execution"
        )
    """
    
    def __init__(self):
        self._email_backend = EmailNotificationBackend()
        self._push_backend = PushNotificationBackend()
    
    async def send(
        self,
        user_id: int,
        channel: NotificationChannel,
        subject: str,
        body: str,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        data: dict | None = None,
    ) -> bool:
        """
        Send a notification.
        
        Args:
            user_id: User to notify
            channel: Notification channel
            subject: Notification subject/title
            body: Notification body/message
            priority: Priority level
            data: Additional data
            
        Returns:
            True if sent successfully
        """
        try:
            if channel == NotificationChannel.EMAIL:
                return await self._email_backend.send(user_id, subject, body, data)
            elif channel == NotificationChannel.PUSH:
                return await self._push_backend.send(user_id, subject, body, data)
            elif channel == NotificationChannel.WEBSOCKET:
                return await self._send_websocket(user_id, subject, body, data)
            else:
                logger.warning(f"Unknown notification channel: {channel}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
            return False
    
    async def send_approval_notification(
        self,
        user_id: int,
        request_id: str,
        title: str,
        message: str,
        workflow_name: str = "",
        channels: list[NotificationChannel] | None = None,
    ) -> list[str]:
        """
        Send approval notification through multiple channels.
        
        Returns list of channels that successfully sent.
        """
        if channels is None:
            channels = [NotificationChannel.WEBSOCKET, NotificationChannel.EMAIL]
        
        data = {
            'request_id': request_id,
            'type': 'approval',
            'workflow_name': workflow_name,
            'action_url': f"/orchestrator/hitl/{request_id}",
        }
        
        body = f"""
{message}

Workflow: {workflow_name or 'N/A'}

Click here to respond: {data['action_url']}

This request will expire if not responded to.
"""
        
        successful = []
        for channel in channels:
            if await self.send(
                user_id=user_id,
                channel=channel,
                subject=f"[Action Required] {title}",
                body=body,
                priority=NotificationPriority.HIGH,
                data=data,
            ):
                successful.append(channel.value)
        
        return successful
    
    async def _send_websocket(
        self,
        user_id: int,
        subject: str,
        body: str,
        data: dict | None,
    ) -> bool:
        """Send via WebSocket."""
        from streaming.consumers import send_hitl_request_to_user
        
        notification_data = {
            'type': 'notification',
            'subject': subject,
            'body': body,
            **(data or {}),
        }
        
        try:
            await send_hitl_request_to_user(user_id, notification_data)
            return True
        except:
            return False


class EmailNotificationBackend:
    """
    Email notification backend.
    
    Uses Django's email system or external provider.
    """
    
    async def send(
        self,
        user_id: int,
        subject: str,
        body: str,
        data: dict | None = None,
    ) -> bool:
        """Send email notification."""
        from asgiref.sync import sync_to_async
        from django.core.mail import send_mail
        from django.conf import settings
        from django.contrib.auth import get_user_model
        
        User = get_user_model()
        
        @sync_to_async
        def get_email():
            try:
                user = User.objects.get(id=user_id)
                return user.email
            except User.DoesNotExist:
                return None
        
        email = await get_email()
        if not email:
            logger.warning(f"No email for user {user_id}")
            return False
        
        @sync_to_async
        def send_email():
            try:
                send_mail(
                    subject=subject,
                    message=body,
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com'),
                    recipient_list=[email],
                    fail_silently=False,
                )
                return True
            except Exception as e:
                logger.error(f"Failed to send email: {e}")
                return False
        
        return await send_email()


class PushNotificationBackend:
    """
    Push notification backend.
    
    Placeholder for Firebase/OneSignal/etc integration.
    """
    
    async def send(
        self,
        user_id: int,
        title: str,
        body: str,
        data: dict | None = None,
    ) -> bool:
        """Send push notification."""
        # TODO: Implement with Firebase/OneSignal
        logger.info(f"Push notification (not implemented): user={user_id}, title={title}")
        return False


class NotificationQueue:
    """
    Queue for async notification processing.
    
    Stores notifications for batch processing or retry.
    """
    
    def __init__(self):
        self._queue: list[dict] = []
    
    def enqueue(
        self,
        user_id: int,
        channel: NotificationChannel,
        subject: str,
        body: str,
        **kwargs
    ) -> str:
        """Add notification to queue."""
        from uuid import uuid4
        
        notification_id = str(uuid4())
        
        self._queue.append({
            'id': notification_id,
            'user_id': user_id,
            'channel': channel.value,
            'subject': subject,
            'body': body,
            'queued_at': datetime.utcnow().isoformat(),
            'status': 'pending',
            **kwargs,
        })
        
        return notification_id
    
    async def process_queue(self, batch_size: int = 10) -> dict:
        """Process queued notifications."""
        service = get_notification_service()
        
        processed = 0
        failed = 0
        
        batch = self._queue[:batch_size]
        
        for notification in batch:
            try:
                success = await service.send(
                    user_id=notification['user_id'],
                    channel=NotificationChannel(notification['channel']),
                    subject=notification['subject'],
                    body=notification['body'],
                    data=notification.get('data'),
                )
                
                if success:
                    processed += 1
                    self._queue.remove(notification)
                else:
                    failed += 1
                    notification['status'] = 'failed'
                    notification['retry_count'] = notification.get('retry_count', 0) + 1
                    
            except Exception as e:
                failed += 1
                notification['error'] = str(e)
        
        return {
            'processed': processed,
            'failed': failed,
            'remaining': len(self._queue),
        }
    
    def get_pending(self, user_id: int | None = None) -> list[dict]:
        """Get pending notifications."""
        if user_id:
            return [n for n in self._queue if n['user_id'] == user_id and n['status'] == 'pending']
        return [n for n in self._queue if n['status'] == 'pending']


# Global instances
_notification_service: NotificationService | None = None
_notification_queue: NotificationQueue | None = None


def get_notification_service() -> NotificationService:
    """Get global notification service."""
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service


def get_notification_queue() -> NotificationQueue:
    """Get global notification queue."""
    global _notification_queue
    if _notification_queue is None:
        _notification_queue = NotificationQueue()
    return _notification_queue
