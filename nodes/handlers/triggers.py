"""
Trigger Node Handlers


Nodes that start workflow execution.
"""
from datetime import datetime
from typing import Any, TYPE_CHECKING
import httpx


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
    outputs = [HandleDef(id="output-0", label="On trigger")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Add a small perceptual delay so the user sees the "Running" state
        import asyncio
        await asyncio.sleep(0.8)
        
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "execution_id": str(context.execution_id),
                "trigger_type": "manual"
            },
            output_handle="output-0"
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
            options=["GET", "POST", "PUT", "DELETE", "PATCH"],
            default="POST"
        ),
        FieldConfig(
            name="path",
            label="Webhook Path",
            field_type=FieldType.STRING,
            placeholder="/my-webhook",
            description="Unique path for this webhook"
        ),
        FieldConfig(
            name="authentication",
            label="Authentication",
            field_type=FieldType.SELECT,
            options=["none", "header", "query"],
            default="none",
            required=False,
            description="Webhook authentication method"
        ),
        FieldConfig(
            name="auth_key",
            label="Auth Key/Header Name",
            field_type=FieldType.STRING,
            required=False,
            placeholder="X-API-Key",
            description="Authentication key or header name"
        ),
    ]
    inputs = []
    outputs = [
        HandleDef(id="output-0", label="On webhook")
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
                "url": input_data.get("url", ""),
            },
            output_handle="output-0"
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
            name="interval_type",
            label="Interval Type",
            field_type=FieldType.SELECT,
            options=["cron", "interval", "days", "hours", "minutes"],
            default="cron",
            description="Schedule interval type"
        ),
        FieldConfig(
            name="cron",
            label="Cron Expression",
            field_type=FieldType.STRING,
            placeholder="0 9 * * *",
            required=False,
            description="Cron expression (e.g., '0 9 * * *' for 9am daily)"
        ),
        FieldConfig(
            name="interval_value",
            label="Interval Value",
            field_type=FieldType.STRING,
            required=False,
            placeholder="30",
            description="Numeric interval (for minutes/hours/days)"
        ),
        FieldConfig(
            name="timezone",
            label="Timezone",
            field_type=FieldType.STRING,
            default="UTC",
            required=False,
            description="Timezone for schedule execution"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On schedule")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        interval_type = config.get("interval_type", "cron")
        
        data = {
            "triggered_at": datetime.now().isoformat(),
            "interval_type": interval_type,
            "timezone": config.get("timezone", "UTC"),
            "trigger_type": "schedule"
        }
        
        if interval_type == "cron":
            data["cron"] = config.get("cron", "")
        else:
            data["interval_value"] = config.get("interval_value", "")
        
        return NodeExecutionResult(
            success=True,
            data=data,
            output_handle="output-0"
        )



class EmailTriggerNode(BaseNodeHandler):
    """
    Email trigger - starts workflow when email is received.
    
    Monitors IMAP mailbox for new emails.
    """
    
    node_type = "email_trigger"
    name = "Email Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on new email"
    icon = "📬"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="credential",
            label="Email Credential",
            field_type=FieldType.CREDENTIAL,
            credential_type="email",
            description="IMAP email account credential"
        ),
        FieldConfig(
            name="mailbox",
            label="Mailbox",
            field_type=FieldType.STRING,
            default="INBOX",
            description="Mailbox to monitor"
        ),
        FieldConfig(
            name="filter_sender",
            label="Filter by Sender",
            field_type=FieldType.STRING,
            required=False,
            placeholder="sender@example.com",
            description="Only trigger for emails from this sender"
        ),
        FieldConfig(
            name="filter_subject",
            label="Filter by Subject",
            field_type=FieldType.STRING,
            required=False,
            placeholder="Important",
            description="Only trigger if subject contains this text"
        ),
        FieldConfig(
            name="mark_as_read",
            label="Mark as Read",
            field_type=FieldType.SELECT,
            options=["true", "false"],
            default="false",
            required=False,
            description="Mark processed emails as read"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On email")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Email data comes from input_data (set by email poller)
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "from": input_data.get("from", ""),
                "to": input_data.get("to", ""),
                "subject": input_data.get("subject", ""),
                "body": input_data.get("body", ""),
                "html_body": input_data.get("html_body", ""),
                "attachments": input_data.get("attachments", []),
                "date": input_data.get("date", ""),
                "message_id": input_data.get("message_id", ""),
            },
            output_handle="output-0"
        )



class FormTriggerNode(BaseNodeHandler):
    """
    Form trigger - starts workflow when form is submitted.
    
    Generates a hosted form and triggers on submission.
    """
    
    node_type = "form_trigger"
    name = "Form Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on form submission"
    icon = "📋"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="form_title",
            label="Form Title",
            field_type=FieldType.STRING,
            default="Submit Form",
            description="Title displayed on the form"
        ),
        FieldConfig(
            name="form_description",
            label="Form Description",
            field_type=FieldType.STRING,
            required=False,
            description="Description text shown on form"
        ),
        FieldConfig(
            name="fields",
            label="Form Fields (JSON)",
            field_type=FieldType.JSON,
            default=[],
            description="Array of form field definitions"
        ),
        FieldConfig(
            name="submit_button_text",
            label="Submit Button Text",
            field_type=FieldType.STRING,
            default="Submit",
            required=False,
            description="Text for submit button"
        ),
        FieldConfig(
            name="success_message",
            label="Success Message",
            field_type=FieldType.STRING,
            default="Thank you for your submission!",
            required=False,
            description="Message shown after successful submission"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On submit")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Form submission data comes from input_data
        return NodeExecutionResult(
            success=True,
            data={
                "submitted_at": datetime.now().isoformat(),
                "form_data": input_data.get("form_data", {}),
                "submitter_ip": input_data.get("ip_address", ""),
                "user_agent": input_data.get("user_agent", ""),
            },
            output_handle="output-0"
        )



class SlackTriggerNode(BaseNodeHandler):
    """
    Slack trigger - starts workflow on Slack events.
    
    Listens for mentions, messages, or reactions.
    """
    
    node_type = "slack_trigger"
    name = "Slack Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on Slack events"
    icon = "💬"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="credential",
            label="Slack Credential",
            field_type=FieldType.CREDENTIAL,
            credential_type="slack",
            description="Slack bot token credential"
        ),
        FieldConfig(
            name="event_type",
            label="Event Type",
            field_type=FieldType.SELECT,
            options=["message", "mention", "reaction_added", "channel_created"],
            default="message",
            description="Type of Slack event to listen for"
        ),
        FieldConfig(
            name="channel_id",
            label="Channel ID",
            field_type=FieldType.STRING,
            required=False,
            placeholder="C1234567890",
            description="Specific channel to monitor (optional)"
        ),
        FieldConfig(
            name="keyword_filter",
            label="Keyword Filter",
            field_type=FieldType.STRING,
            required=False,
            placeholder="urgent",
            description="Only trigger if message contains this keyword"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On event")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Slack event data comes from input_data (set by Slack event handler)
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "event_type": config.get("event_type", "message"),
                "channel": input_data.get("channel", ""),
                "user": input_data.get("user", ""),
                "text": input_data.get("text", ""),
                "timestamp": input_data.get("ts", ""),
                "thread_ts": input_data.get("thread_ts", ""),
                "event_data": input_data,
            },
            output_handle="output-0"
        )



class GoogleSheetsTriggerNode(BaseNodeHandler):
    """
    Google Sheets trigger - starts workflow when rows are added/updated.
    
    Polls Google Sheets for changes.
    """
    
    node_type = "google_sheets_trigger"
    name = "Google Sheets Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on sheet changes"
    icon = "📊"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="credential",
            label="Google Credential",
            field_type=FieldType.CREDENTIAL,
            credential_type="google-oauth2",
            description="Google OAuth credential with Sheets access"
        ),
        FieldConfig(
            name="spreadsheet_id",
            label="Spreadsheet ID",
            field_type=FieldType.STRING,
            placeholder="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
            description="Google Sheets spreadsheet ID"
        ),
        FieldConfig(
            name="sheet_name",
            label="Sheet Name",
            field_type=FieldType.STRING,
            default="Sheet1",
            description="Name of the sheet to monitor"
        ),
        FieldConfig(
            name="trigger_on",
            label="Trigger On",
            field_type=FieldType.SELECT,
            options=["new_row", "updated_row", "any_change"],
            default="new_row",
            description="Type of change to trigger on"
        ),
        FieldConfig(
            name="poll_interval",
            label="Poll Interval (minutes)",
            field_type=FieldType.STRING,
            default="5",
            required=False,
            description="How often to check for changes"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On change")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Sheet change data comes from input_data (set by polling service)
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "spreadsheet_id": config.get("spreadsheet_id", ""),
                "sheet_name": config.get("sheet_name", "Sheet1"),
                "trigger_type": config.get("trigger_on", "new_row"),
                "row_number": input_data.get("row_number", 0),
                "row_data": input_data.get("row_data", {}),
                "change_type": input_data.get("change_type", ""),
            },
            output_handle="output-0"
        )



class GitHubTriggerNode(BaseNodeHandler):
    """
    GitHub trigger - starts workflow on GitHub events.
    
    Listens for push, PR, issues, and other GitHub events.
    """
    
    node_type = "github_trigger"
    name = "GitHub Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on GitHub events"
    icon = "🐙"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="credential",
            label="GitHub Token",
            field_type=FieldType.CREDENTIAL,
            credential_type="github",
            description="GitHub personal access token"
        ),
        FieldConfig(
            name="repository",
            label="Repository",
            field_type=FieldType.STRING,
            placeholder="owner/repo",
            description="GitHub repository (format: owner/repo)"
        ),
        FieldConfig(
            name="events",
            label="Events (JSON)",
            field_type=FieldType.JSON,
            default=["push", "pull_request"],
            description="Array of GitHub events to listen for"
        ),
        FieldConfig(
            name="branch_filter",
            label="Branch Filter",
            field_type=FieldType.STRING,
            required=False,
            placeholder="main",
            description="Only trigger for specific branch"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On event")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # GitHub webhook data comes from input_data
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "repository": config.get("repository", ""),
                "event": input_data.get("event", ""),
                "action": input_data.get("action", ""),
                "sender": input_data.get("sender", {}),
                "ref": input_data.get("ref", ""),
                "payload": input_data.get("payload", {}),
            },
            output_handle="output-0"
        )



class DiscordTriggerNode(BaseNodeHandler):
    """
    Discord trigger - starts workflow on Discord events.
    
    Listens for messages, reactions, or member events via webhook.
    """
    
    node_type = "discord_trigger"
    name = "Discord Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on Discord events"
    icon = "🎮"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="bot_token",
            label="Bot Token",
            field_type=FieldType.CREDENTIAL,
            credential_type="discord_bot",
            description="Discord bot token credential"
        ),
        FieldConfig(
            name="event_type",
            label="Event Type",
            field_type=FieldType.SELECT,
            options=["message", "reaction_add", "member_join", "member_leave"],
            default="message",
            description="Type of Discord event to listen for"
        ),
        FieldConfig(
            name="channel_id",
            label="Channel ID",
            field_type=FieldType.STRING,
            required=False,
            placeholder="123456789012345678",
            description="Specific channel to monitor (optional)"
        ),
        FieldConfig(
            name="command_prefix",
            label="Command Prefix",
            field_type=FieldType.STRING,
            required=False,
            placeholder="!",
            description="Only trigger on messages starting with this prefix"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On event")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Discord event data comes from input_data
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "event_type": config.get("event_type", "message"),
                "channel_id": input_data.get("channel_id", ""),
                "guild_id": input_data.get("guild_id", ""),
                "author": input_data.get("author", {}),
                "content": input_data.get("content", ""),
                "timestamp": input_data.get("timestamp", ""),
                "event_data": input_data,
            },
            output_handle="output-0"
        )



class TelegramTriggerNode(BaseNodeHandler):
    """
    Telegram trigger - starts workflow on Telegram messages.
    
    Listens for bot messages, commands, or callbacks.
    """
    
    node_type = "telegram_trigger"
    name = "Telegram Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on Telegram messages"
    icon = "✈️"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="credential",
            label="Bot Token",
            field_type=FieldType.CREDENTIAL,
            credential_type="telegram",
            description="Telegram bot token credential"
        ),
        FieldConfig(
            name="trigger_on",
            label="Trigger On",
            field_type=FieldType.SELECT,
            options=["message", "command", "callback_query", "edited_message"],
            default="message",
            description="Type of update to trigger on"
        ),
        FieldConfig(
            name="command",
            label="Command",
            field_type=FieldType.STRING,
            required=False,
            placeholder="start",
            description="Specific command to listen for (without /)"
        ),
        FieldConfig(
            name="chat_type",
            label="Chat Type",
            field_type=FieldType.SELECT,
            options=["all", "private", "group", "channel"],
            default="all",
            required=False,
            description="Filter by chat type"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On update")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Telegram update data comes from input_data
        # Standardize extraction based on update type
        message = input_data.get("message", {})
        # Handle callback queries or edited messages if present
        if not message:
            message = input_data.get("edited_message", {})
        if not message and "callback_query" in input_data:
            message = input_data["callback_query"].get("message", {})
            
        chat = message.get("chat", {})
        user = message.get("from", {})
        # For direct message updates, 'from' is at top level of message
        # For callbacks, user is 'from' in callback_query
        if "callback_query" in input_data:
            user = input_data["callback_query"].get("from", {})
            
        text = message.get("text", "")
        # If no text (e.g. photo), check caption
        if not text:
            text = message.get("caption", "")
        
        # Command parsing
        command = ""
        args = ""
        if text and text.startswith("/"):
            parts = text.split(" ", 1)
            # Handle /command@botname syntax
            cmd_part = parts[0].replace("/", "")
            if "@" in cmd_part:
                cmd_part = cmd_part.split("@")[0]
            command = cmd_part
            args = parts[1] if len(parts) > 1 else ""
            
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "update_id": input_data.get("update_id", ""),
                "chat_id": chat.get("id"),
                "text": text,
                "command": command,
                "args": args,
                "user": {
                    "id": user.get("id"),
                    "username": user.get("username"),
                    "first_name": user.get("first_name"),
                    "last_name": user.get("last_name"),
                    "language_code": user.get("language_code"),
                },
                "chat": chat,
                # Include full raw objects for advanced use
                "message": message,
                "raw_update": input_data,
                "trigger_type": config.get("trigger_on", "message"),
            },
            output_handle="output-0"
        )



class RssFeedTriggerNode(BaseNodeHandler):
    """
    RSS Feed trigger - starts workflow on new feed items.
    
    Polls RSS/Atom feeds for new entries.
    """
    
    node_type = "rss_feed_trigger"
    name = "RSS Feed Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on new RSS items"
    icon = "📡"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="feed_url",
            label="Feed URL",
            field_type=FieldType.STRING,
            placeholder="https://example.com/feed.xml",
            description="URL of the RSS/Atom feed"
        ),
        FieldConfig(
            name="poll_interval",
            label="Poll Interval (minutes)",
            field_type=FieldType.STRING,
            default="15",
            description="How often to check for new items"
        ),
        FieldConfig(
            name="title_filter",
            label="Title Filter",
            field_type=FieldType.STRING,
            required=False,
            description="Only trigger if title contains this text"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On new item")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # RSS item data comes from input_data (set by RSS poller)
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "feed_url": config.get("feed_url", ""),
                "title": input_data.get("title", ""),
                "link": input_data.get("link", ""),
                "description": input_data.get("description", ""),
                "published": input_data.get("published", ""),
                "author": input_data.get("author", ""),
                "content": input_data.get("content", ""),
            },
            output_handle="output"
        )



class FileTriggerNode(BaseNodeHandler):
    """
    File trigger - starts workflow when file is created/modified.
    
    Monitors a directory or cloud storage for file changes.
    """
    
    node_type = "file_trigger"
    name = "File Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on file changes"
    icon = "📁"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="watch_path",
            label="Watch Path",
            field_type=FieldType.STRING,
            placeholder="/uploads",
            description="Directory path to monitor"
        ),
        FieldConfig(
            name="trigger_on",
            label="Trigger On",
            field_type=FieldType.SELECT,
            options=["created", "modified", "deleted", "any"],
            default="created",
            description="File event to trigger on"
        ),
        FieldConfig(
            name="file_pattern",
            label="File Pattern",
            field_type=FieldType.STRING,
            required=False,
            placeholder="*.pdf",
            description="File name pattern (glob syntax)"
        ),
        FieldConfig(
            name="recursive",
            label="Recursive",
            field_type=FieldType.SELECT,
            options=["true", "false"],
            default="false",
            required=False,
            description="Watch subdirectories recursively"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output", label="On file change")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # File event data comes from input_data (set by file watcher)
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "event_type": input_data.get("event_type", ""),
                "file_path": input_data.get("file_path", ""),
                "file_name": input_data.get("file_name", ""),
                "file_size": input_data.get("file_size", 0),
                "modified_time": input_data.get("modified_time", ""),
                "file_extension": input_data.get("file_extension", ""),
            },
            output_handle="output"
        )



class SQSTriggerNode(BaseNodeHandler):
    """
    AWS SQS trigger - starts workflow on SQS queue messages.
    
    Polls AWS SQS queue for new messages.
    """
    
    node_type = "sqs_trigger"
    name = "SQS Trigger"
    category = NodeCategory.TRIGGER.value
    description = "Start workflow on SQS messages"
    icon = "☁️"
    color = "#22c55e"
    
    fields = [
        FieldConfig(
            name="credential",
            label="AWS Credential",
            field_type=FieldType.CREDENTIAL,
            description="AWS access credentials"
        ),
        FieldConfig(
            name="queue_url",
            label="Queue URL",
            field_type=FieldType.STRING,
            placeholder="https://sqs.region.amazonaws.com/account/queue",
            description="SQS queue URL"
        ),
        FieldConfig(
            name="max_messages",
            label="Max Messages",
            field_type=FieldType.STRING,
            default="1",
            required=False,
            description="Maximum messages to receive per poll (1-10)"
        ),
        FieldConfig(
            name="visibility_timeout",
            label="Visibility Timeout (seconds)",
            field_type=FieldType.STRING,
            default="30",
            required=False,
            description="How long to hide message from other consumers"
        ),
        FieldConfig(
            name="delete_after_process",
            label="Delete After Processing",
            field_type=FieldType.SELECT,
            options=["true", "false"],
            default="true",
            required=False,
            description="Automatically delete message after successful processing"
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output", label="On message")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # SQS message data comes from input_data (set by SQS poller)
        return NodeExecutionResult(
            success=True,
            data={
                "triggered_at": datetime.now().isoformat(),
                "message_id": input_data.get("message_id", ""),
                "receipt_handle": input_data.get("receipt_handle", ""),
                "body": input_data.get("body", ""),
                "attributes": input_data.get("attributes", {}),
                "message_attributes": input_data.get("message_attributes", {}),
                "sent_timestamp": input_data.get("sent_timestamp", ""),
            },
            output_handle="output"
        )
