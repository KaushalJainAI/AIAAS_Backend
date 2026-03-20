"""
Utility Node Handlers

General purpose nodes for system actions, notifications, and workflow control.
"""
from typing import Any, TYPE_CHECKING
from .base import (
    BaseNodeHandler,
    NodeCategory,
    FieldConfig,
    FieldType,
    HandleDef,
    NodeExecutionResult,
    NodeItem,
)

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext

class NotificationNode(BaseNodeHandler):
    """
    Send a real-time notification to the user interface.
    
    Displays a message in the activity timeline or as a global notification.
    """
    
    node_type = "notification"
    name = "Notify User"
    category = NodeCategory.UTILITY.value
    description = "Send a notification to the UI"
    icon = "🔔"
    color = "#f59e0b"  # Amber
    
    fields = [
        FieldConfig(
            name="message",
            label="Message",
            field_type=FieldType.STRING,
            placeholder="Notification message...",
            description="The message to display to the user"
        ),
        FieldConfig(
            name="level",
            label="Level",
            field_type=FieldType.SELECT,
            options=["info", "success", "warning", "error"],
            default="info"
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Continue"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        message = config.get("message", "")
        level = config.get("level", "info")
        
        if not message:
            return NodeExecutionResult(
                success=False,
                error="Notification message is required",
                output_handle="output-0"
            )
            
        # Broadcast to the user via SSE
        from streaming.broadcaster import get_broadcaster
        broadcaster = get_broadcaster()
        
        from datetime import datetime, timezone
        await broadcaster.send_event(
            context.execution_id,
            "user_notification",
            {
                "message": message,
                "level": level,
                "node_id": context.current_node_id,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        )
        
        return NodeExecutionResult(
            success=True,
            items=[NodeItem(json={"notification_sent": True, "message": message, "level": level})],
            output_handle="output-0"
        )


class SendNotificationNode(BaseNodeHandler):
    """
    Send an asynchronous notification to a user.
    
    Displays a message via Email, Push, WebSocket, or SMS.
    """
    
    node_type = "send_notification"
    name = "Send Notification"
    category = NodeCategory.UTILITY.value
    description = "Send a notification via Email, Push, etc."
    icon = "📨"
    color = "#8b5cf6"  # Purple
    
    fields = [
        FieldConfig(
            name="channel",
            label="Channel",
            field_type=FieldType.SELECT,
            options=["email", "push", "websocket", "sms"],
            default="email",
            description="The channel to send the notification through"
        ),
        FieldConfig(
            name="target_user_id",
            label="Target User ID",
            field_type=FieldType.STRING,
            required=False,
            description="User ID to notify. Defaults to workflow runner if empty."
        ),
        FieldConfig(
            name="subject",
            label="Subject / Title",
            field_type=FieldType.STRING,
            placeholder="Notification Subject",
            description="The subject or title of the notification"
        ),
        FieldConfig(
            name="body",
            label="Message Body",
            field_type=FieldType.STRING,
            placeholder="Type your message here...",
            description="The content of the notification"
        ),
        FieldConfig(
            name="priority",
            label="Priority",
            field_type=FieldType.SELECT,
            options=["low", "normal", "high", "urgent"],
            default="normal",
            description="Notification priority level"
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Success"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        from orchestrator.notifications import (
            get_notification_service, 
            NotificationChannel, 
            NotificationPriority
        )
        
        channel_str = config.get("channel", "email")
        target_user_id_str = config.get("target_user_id", "")
        subject = config.get("subject", "")
        body = config.get("body", "")
        priority_str = config.get("priority", "normal")
        
        if not subject or not body:
            return NodeExecutionResult(
                success=False,
                error="Subject and Body are required",
                output_handle="output-0"
            )
            
        try:
            channel = NotificationChannel(channel_str)
        except ValueError:
            return NodeExecutionResult(
                success=False,
                error=f"Invalid channel: {channel_str}",
                output_handle="output-0"
            )
            
        try:
            priority = NotificationPriority(priority_str)
        except ValueError:
            priority = NotificationPriority.NORMAL
            
        # Determine target user
        target_user_id = context.user_id
        if target_user_id_str:
            try:
                target_user_id = int(target_user_id_str)
            except ValueError:
                return NodeExecutionResult(
                    success=False,
                    error=f"Invalid target_user_id: {target_user_id_str}. Must be an integer.",
                    output_handle="output-0"
                )
                
        service = get_notification_service()
        success = await service.send(
            user_id=target_user_id,
            channel=channel,
            subject=subject,
            body=body,
            priority=priority,
            data={"node_id": context.current_node_id, "execution_id": str(context.execution_id)}
        )
        
        if success:
            return NodeExecutionResult(
                success=True,
                items=[NodeItem(json={
                    "status": "sent",
                    "channel": channel.value,
                    "target_user_id": target_user_id,
                    "subject": subject,
                })],
                output_handle="output-0"
            )
        else:
            return NodeExecutionResult(
                success=False,
                error=f"Failed to send notification via {channel.value}",
                output_handle="output-0"
            )
