"""
Trigger Node Handlers

Nodes that start workflow execution.
"""
from datetime import datetime
from typing import Any, TYPE_CHECKING

from .base import (
    BaseNodeHandler,
    NodeCategory,
    FieldConfig,
    FieldType,
    HandleDef,
    NodeExecutionResult,
)

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext


class ManualTriggerNode(BaseNodeHandler):
    """
    Manual trigger - starts workflow on user action.
    
    No inputs, outputs execution start timestamp.
    """
    
    node_type = "manual_trigger"
    name = "Manual Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Manually start this workflow"
    icon = "▶️"
    color = "#22c55e"  # Green
    
    fields = []
    inputs = []  # Triggers have no inputs
    outputs = [HandleDef(id="output", label="On trigger")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "execution_id": str(context.execution_id),
                "trigger_type": "manual"
            }
        )


class WebhookTriggerNode(BaseNodeHandler):
    """
    Webhook trigger - starts workflow on HTTP request.
    
    Outputs the incoming request data.
    """
    
    node_type = "webhook_trigger"
    name = "Webhook Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow when webhook is called"
    icon = "🔗"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="method",
            label="HTTP Method",
            field_type=FieldType.SELECT,
            options=["GET", "POST", "PUT", "DELETE"],
            default="POST"
        ),
        FieldConfig(
            name="path",
            label="Webhook Path",
            field_type=FieldType.STRING,
            placeholder="/my-webhook",
            description="Unique path for this webhook"
        ),
    ]
    inputs = []
    outputs = [
        HandleDef(id="output", label="On webhook")
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Webhook data comes from input_data (set by webhook handler)
        return NodeExecutionResult(
            success=True,
            data={
                "received_at": datetime.now().isoformat(),
                "method": config.get("method", "POST"),
                "path": config.get("path", ""),
                "headers": input_data.get("headers", {}),
                "body": input_data.get("body", {}),
                "query": input_data.get("query", {}),
            }
        )


class ScheduleTriggerNode(BaseNodeHandler):
    """
    Schedule trigger - starts workflow on a schedule.
    
    Outputs execution timestamp and schedule info.
    """
    
    node_type = "schedule_trigger"
    name = "Schedule Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on a schedule"
    icon = "⏰"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="cron",
            label="Cron Expression",
            field_type=FieldType.STRING,
            placeholder="0 9 * * *",
            description="Cron expression (e.g., '0 9 * * *' for 9am daily)"
        ),
        FieldConfig(
            name="timezone",
            label="Timezone",
            field_type=FieldType.STRING,
            default="UTC",
            required=False
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output", label="On schedule")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "cron": config.get("cron", ""),
                "timezone": config.get("timezone", "UTC"),
                "trigger_type": "schedule"
            }
        )
