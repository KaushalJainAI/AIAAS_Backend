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
        
        from datetime import datetime
        await broadcaster.send_event(
            context.execution_id,
            "user_notification",
            {
                "message": message,
                "level": level,
                "node_id": context.current_node_id,
                "timestamp": datetime.utcnow().isoformat()
            }
        )
        
        return NodeExecutionResult(
            success=True,
            data={"notification_sent": True, "message": message, "level": level},
            output_handle="output-0"
        )
