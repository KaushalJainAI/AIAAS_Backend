"""
Integration Node Handlers


Third-party service integration nodes for Gmail, Slack, Google Sheets, and more.
"""
import httpx
from typing import Any, TYPE_CHECKING
from urllib.parse import quote
import json


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



class GmailNode(BaseNodeHandler):
    """
    Send emails using Gmail API.
    
    Requires OAuth2 credential with Gmail send scope.
    """
    
    node_type = "gmail"
    name = "Gmail"
    category = NodeCategory.INTEGRATION.value
    description = "Send emails via Gmail"
    icon = "📧"
    color = "#ea4335"  # Gmail red
    
    fields = [
        FieldConfig(
            name="credential",
            label="Gmail Credential",
            field_type=FieldType.CREDENTIAL,
            description="Select your Gmail OAuth credential"
        ),
        FieldConfig(
            name="operation",
            label="Operation",
            field_type=FieldType.SELECT,
            options=["send_email", "create_draft"],
            default="send_email"
        ),
        FieldConfig(
            name="to",
            label="To",
            field_type=FieldType.STRING,
            placeholder="recipient@example.com",
            description="Recipient email address(es), comma-separated"
        ),
        FieldConfig(
            name="subject",
            label="Subject",
            field_type=FieldType.STRING,
            placeholder="Email subject",
            description="Email subject line"
        ),
        FieldConfig(
            name="body",
            label="Body",
            field_type=FieldType.STRING,
            placeholder="Email content...",
            description="Plain text email body"
        ),
        FieldConfig(
            name="html_body",
            label="HTML Body",
            field_type=FieldType.STRING,
            required=False,
            description="Optional HTML email body"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        import base64
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        credential_id = config.get("credential")
        operation = config.get("operation", "send_email")
        to = config.get("to", "")
        subject = config.get("subject", "")
        body = config.get("body", "")
        html_body = config.get("html_body", "")
        
        if not to or not subject:
            return NodeExecutionResult(
                success=False,
                error="To and Subject are required",
                output_handle="error"
            )
        
        # Get OAuth access token
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "access_token" not in creds:
            return NodeExecutionResult(
                success=False,
                error="Gmail OAuth credential not configured",
                output_handle="error"
            )
        
        access_token = creds["access_token"]
        
        try:
            # Create email message
            if html_body:
                message = MIMEMultipart("alternative")
                message.attach(MIMEText(body, "plain", "utf-8"))
                message.attach(MIMEText(html_body, "html", "utf-8"))
            else:
                message = MIMEText(body, "plain", "utf-8")
            
            message["To"] = to
            message["From"] = "me"
            message["Subject"] = subject
            
            # Encode message
            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
            
            async with httpx.AsyncClient(timeout=30) as client:
                if operation == "send_email":
                    endpoint = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
                    payload = {"raw": raw_message}
                else:  # create_draft
                    endpoint = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
                    payload = {"message": {"raw": raw_message}}
                
                response = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                
                if response.status_code not in (200, 201):
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("error", {}).get("message", response.text)
                    except Exception:
                        error_msg = response.text
                    return NodeExecutionResult(
                        success=False,
                        error=f"Gmail API error: {error_msg}",
                        output_handle="error"
                    )
                
                data = response.json()
                return NodeExecutionResult(
                    success=True,
                    data={
                        "message_id": data.get("id"),
                        "thread_id": data.get("threadId"),
                        "operation": operation,
                        "to": to,
                        "subject": subject,
                    },
                    output_handle="success"
                )
                
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Gmail error: {str(e)}",
                output_handle="error"
            )



class SlackNode(BaseNodeHandler):
    """
    Send messages to Slack channels.
    
    Uses Slack Bot Token for authentication.
    """
    
    node_type = "slack"
    name = "Slack"
    category = NodeCategory.INTEGRATION.value
    description = "Send messages to Slack"
    icon = "💬"
    color = "#4a154b"  # Slack purple
    
    fields = [
        FieldConfig(
            name="credential",
            label="Slack Bot Token",
            field_type=FieldType.CREDENTIAL,
            description="Select your Slack credential"
        ),
        FieldConfig(
            name="operation",
            label="Operation",
            field_type=FieldType.SELECT,
            options=["send_message", "reply_to_thread"],
            default="send_message"
        ),
        FieldConfig(
            name="channel",
            label="Channel",
            field_type=FieldType.STRING,
            placeholder="#general or C1234567890",
            description="Channel name or ID"
        ),
        FieldConfig(
            name="message",
            label="Message",
            field_type=FieldType.STRING,
            placeholder="Hello from workflow!",
            description="Message text (supports Slack markdown)"
        ),
        FieldConfig(
            name="thread_ts",
            label="Thread Timestamp",
            field_type=FieldType.STRING,
            required=False,
            description="Reply to thread (timestamp of parent message)"
        ),
        FieldConfig(
            name="blocks",
            label="Blocks (JSON)",
            field_type=FieldType.JSON,
            required=False,
            default=[],
            description="Optional Block Kit JSON for rich formatting"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        operation = config.get("operation", "send_message")
        channel = config.get("channel", "")
        message = config.get("message", "")
        thread_ts = config.get("thread_ts", "")
        blocks = config.get("blocks", [])
        
        if not channel or not message:
            return NodeExecutionResult(
                success=False,
                error="Channel and Message are required",
                output_handle="error"
            )
        
        if operation == "reply_to_thread" and not thread_ts:
            return NodeExecutionResult(
                success=False,
                error="thread_ts is required for reply_to_thread operation",
                output_handle="error"
            )
        
        # Get bot token
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "bot_token" not in creds:
            return NodeExecutionResult(
                success=False,
                error="Slack bot token not configured",
                output_handle="error"
            )
        
        bot_token = creds["bot_token"]
        
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                payload = {
                    "channel": channel,
                    "text": message,
                }
                
                if thread_ts:
                    payload["thread_ts"] = thread_ts
                
                if blocks:
                    payload["blocks"] = blocks
                
                response = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={
                        "Authorization": f"Bearer {bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=payload,
                )
                
                if response.status_code != 200:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Slack HTTP error: {response.text}",
                        output_handle="error"
                    )
                
                data = response.json()
                
                if not data.get("ok"):
                    return NodeExecutionResult(
                        success=False,
                        error=f"Slack API error: {data.get('error', 'Unknown error')}",
                        output_handle="error"
                    )
                
                return NodeExecutionResult(
                    success=True,
                    data={
                        "message_ts": data.get("ts"),
                        "channel": data.get("channel"),
                        "thread_ts": data.get("message", {}).get("thread_ts"),
                    },
                    output_handle="success"
                )
                
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Slack error: {str(e)}",
                output_handle="error"
            )



class GoogleSheetsNode(BaseNodeHandler):
    """
    Read/write data to Google Sheets.
    
    Requires OAuth2 credential with Sheets API scope.
    """
    
    node_type = "google_sheets"
    name = "Google Sheets"
    category = NodeCategory.INTEGRATION.value
    description = "Read or write Google Sheets data"
    icon = "📊"
    color = "#0f9d58"  # Sheets green
    
    fields = [
        FieldConfig(
            name="credential",
            label="Google Credential",
            field_type=FieldType.CREDENTIAL,
            description="Select your Google OAuth credential"
        ),
        FieldConfig(
            name="operation",
            label="Operation",
            field_type=FieldType.SELECT,
            options=["read_range", "write_range", "append_rows", "clear_range"],
            default="read_range"
        ),
        FieldConfig(
            name="spreadsheet_id",
            label="Spreadsheet ID",
            field_type=FieldType.STRING,
            placeholder="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
            description="ID from the spreadsheet URL"
        ),
        FieldConfig(
            name="range",
            label="Range",
            field_type=FieldType.STRING,
            placeholder="Sheet1!A1:D10",
            default="Sheet1!A1:Z1000",
            description="A1 notation range (e.g., Sheet1!A1:D10)"
        ),
        FieldConfig(
            name="values",
            label="Values",
            field_type=FieldType.JSON,
            required=False,
            default=[],
            description="2D array for write/append operations"
        ),
        FieldConfig(
            name="value_input_option",
            label="Value Input Option",
            field_type=FieldType.SELECT,
            options=["RAW", "USER_ENTERED"],
            default="USER_ENTERED",
            required=False,
            description="How input data should be interpreted"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        operation = config.get("operation", "read_range")
        spreadsheet_id = config.get("spreadsheet_id", "")
        range_notation = config.get("range", "Sheet1!A1:Z1000")
        values = config.get("values", [])
        value_input_option = config.get("value_input_option", "USER_ENTERED")
        
        if not spreadsheet_id:
            return NodeExecutionResult(
                success=False,
                error="Spreadsheet ID is required",
                output_handle="error"
            )
        
        # Get OAuth access token
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "access_token" not in creds:
            return NodeExecutionResult(
                success=False,
                error="Google OAuth credential not configured",
                output_handle="error"
            )
        
        access_token = creds["access_token"]
        base_url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
        safe_range = quote(range_notation, safe="")
        
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                }
                
                if operation == "read_range":
                    response = await client.get(
                        f"{base_url}/values/{safe_range}",
                        headers=headers,
                    )
                    
                    if response.status_code != 200:
                        try:
                            error_data = response.json()
                            error_msg = error_data.get("error", {}).get("message", response.text)
                        except Exception:
                            error_msg = response.text
                        return NodeExecutionResult(
                            success=False,
                            error=f"Sheets API error: {error_msg}",
                            output_handle="error"
                        )
                    
                    data = response.json()
                    return NodeExecutionResult(
                        success=True,
                        data={
                            "values": data.get("values", []),
                            "range": data.get("range"),
                            "row_count": len(data.get("values", [])),
                        },
                        output_handle="success"
                    )
                
                elif operation == "write_range":
                    if not values:
                        return NodeExecutionResult(
                            success=False,
                            error="Values are required for write operation",
                            output_handle="error"
                        )
                    
                    response = await client.put(
                        f"{base_url}/values/{safe_range}",
                        headers=headers,
                        params={"valueInputOption": value_input_option},
                        json={"values": values},
                    )
                    
                    if response.status_code != 200:
                        try:
                            error_data = response.json()
                            error_msg = error_data.get("error", {}).get("message", response.text)
                        except Exception:
                            error_msg = response.text
                        return NodeExecutionResult(
                            success=False,
                            error=f"Sheets API error: {error_msg}",
                            output_handle="error"
                        )
                    
                    data = response.json()
                    return NodeExecutionResult(
                        success=True,
                        data={
                            "updated_range": data.get("updatedRange"),
                            "updated_rows": data.get("updatedRows"),
                            "updated_cells": data.get("updatedCells"),
                        },
                        output_handle="success"
                    )
                
                elif operation == "append_rows":
                    if not values:
                        return NodeExecutionResult(
                            success=False,
                            error="Values are required for append operation",
                            output_handle="error"
                        )
                    
                    response = await client.post(
                        f"{base_url}/values/{safe_range}:append",
                        headers=headers,
                        params={
                            "valueInputOption": value_input_option,
                            "insertDataOption": "INSERT_ROWS",
                        },
                        json={"values": values},
                    )
                    
                    if response.status_code != 200:
                        try:
                            error_data = response.json()
                            error_msg = error_data.get("error", {}).get("message", response.text)
                        except Exception:
                            error_msg = response.text
                        return NodeExecutionResult(
                            success=False,
                            error=f"Sheets API error: {error_msg}",
                            output_handle="error"
                        )
                    
                    data = response.json()
                    updates = data.get("updates", {})
                    return NodeExecutionResult(
                        success=True,
                        data={
                            "updated_range": updates.get("updatedRange"),
                            "updated_rows": updates.get("updatedRows"),
                        },
                        output_handle="success"
                    )
                
                elif operation == "clear_range":
                    response = await client.post(
                        f"{base_url}/values/{safe_range}:clear",
                        headers=headers,
                    )
                    
                    if response.status_code != 200:
                        try:
                            error_data = response.json()
                            error_msg = error_data.get("error", {}).get("message", response.text)
                        except Exception:
                            error_msg = response.text
                        return NodeExecutionResult(
                            success=False,
                            error=f"Sheets API error: {error_msg}",
                            output_handle="error"
                        )
                    
                    data = response.json()
                    return NodeExecutionResult(
                        success=True,
                        data={
                            "cleared_range": data.get("clearedRange"),
                        },
                        output_handle="success"
                    )
                
                else:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Unknown operation: {operation}",
                        output_handle="error"
                    )
                    
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Google Sheets error: {str(e)}",
                output_handle="error"
            )



class DiscordNode(BaseNodeHandler):
    """
    Send messages to Discord channels via webhooks.
    
    Supports embeds, files, and rich formatting.
    """
    
    node_type = "discord"
    name = "Discord"
    category = NodeCategory.INTEGRATION.value
    description = "Send messages to Discord"
    icon = "🎮"
    color = "#5865F2"  # Discord blurple
    
    fields = [
        FieldConfig(
            name="webhook_url",
            label="Webhook URL",
            field_type=FieldType.STRING,
            placeholder="https://discord.com/api/webhooks/...",
            description="Discord webhook URL"
        ),
        FieldConfig(
            name="content",
            label="Content",
            field_type=FieldType.STRING,
            placeholder="Message content",
            required=False,
            description="Text content of the message"
        ),
        FieldConfig(
            name="username",
            label="Username",
            field_type=FieldType.STRING,
            required=False,
            description="Override default webhook username"
        ),
        FieldConfig(
            name="avatar_url",
            label="Avatar URL",
            field_type=FieldType.STRING,
            required=False,
            description="Override default webhook avatar"
        ),
        FieldConfig(
            name="embeds",
            label="Embeds (JSON)",
            field_type=FieldType.JSON,
            required=False,
            default=[],
            description="Discord embed objects for rich formatting"
        ),
        FieldConfig(
            name="tts",
            label="Text-to-Speech",
            field_type=FieldType.SELECT,
            options=["false", "true"],
            default="false",
            required=False,
            description="Enable text-to-speech"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        webhook_url = config.get("webhook_url", "")
        content = config.get("content", "")
        username = config.get("username", "")
        avatar_url = config.get("avatar_url", "")
        embeds = config.get("embeds", [])
        tts = config.get("tts", "false") == "true"
        
        if not webhook_url:
            return NodeExecutionResult(
                success=False,
                error="Webhook URL is required",
                output_handle="error"
            )
        
        if not content and not embeds:
            return NodeExecutionResult(
                success=False,
                error="Either content or embeds must be provided",
                output_handle="error"
            )
        
        try:
            payload = {}
            
            if content:
                payload["content"] = content
            if username:
                payload["username"] = username
            if avatar_url:
                payload["avatar_url"] = avatar_url
            if embeds:
                payload["embeds"] = embeds
            if tts:
                payload["tts"] = tts
            
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    webhook_url,
                    json=payload,
                )
                
                if response.status_code not in (200, 204):
                    return NodeExecutionResult(
                        success=False,
                        error=f"Discord API error: {response.text}",
                        output_handle="error"
                    )
                
                return NodeExecutionResult(
                    success=True,
                    data={
                        "status": "sent",
                        "has_embeds": len(embeds) > 0,
                        "tts_enabled": tts,
                    },
                    output_handle="success"
                )
                
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Discord error: {str(e)}",
                output_handle="error"
            )



class NotionNode(BaseNodeHandler):
    """
    Interact with Notion databases and pages.
    
    Create, read, and update Notion content.
    """
    
    node_type = "notion"
    name = "Notion"
    category = NodeCategory.INTEGRATION.value
    description = "Interact with Notion databases"
    icon = "📝"
    color = "#000000"  # Notion black
    
    fields = [
        FieldConfig(
            name="credential",
            label="Notion API Key",
            field_type=FieldType.CREDENTIAL,
            description="Select your Notion integration credential"
        ),
        FieldConfig(
            name="operation",
            label="Operation",
            field_type=FieldType.SELECT,
            options=["create_page", "get_page", "update_page", "query_database", "create_database_item"],
            default="create_page"
        ),
        FieldConfig(
            name="database_id",
            label="Database ID",
            field_type=FieldType.STRING,
            required=False,
            placeholder="a1b2c3d4...",
            description="Notion database ID (for database operations)"
        ),
        FieldConfig(
            name="page_id",
            label="Page ID",
            field_type=FieldType.STRING,
            required=False,
            placeholder="a1b2c3d4...",
            description="Notion page ID (for page operations)"
        ),
        FieldConfig(
            name="properties",
            label="Properties (JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={},
            description="Page/database item properties"
        ),
        FieldConfig(
            name="filter",
            label="Filter (JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={},
            description="Database query filter"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        operation = config.get("operation", "create_page")
        database_id = config.get("database_id", "")
        page_id = config.get("page_id", "")
        properties = config.get("properties", {})
        filter_obj = config.get("filter", {})
        
        # Get API key
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "api_key" not in creds:
            return NodeExecutionResult(
                success=False,
                error="Notion API key not configured",
                output_handle="error"
            )
        
        api_key = creds["api_key"]
        
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Notion-Version": "2022-06-28",
                }
                
                if operation == "create_page":
                    if not database_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Database ID is required for create_page",
                            output_handle="error"
                        )
                    
                    payload = {
                        "parent": {"database_id": database_id},
                        "properties": properties,
                    }
                    
                    response = await client.post(
                        "https://api.notion.com/v1/pages",
                        headers=headers,
                        json=payload,
                    )
                    
                elif operation == "get_page":
                    if not page_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Page ID is required for get_page",
                            output_handle="error"
                        )
                    
                    response = await client.get(
                        f"https://api.notion.com/v1/pages/{page_id}",
                        headers=headers,
                    )
                    
                elif operation == "update_page":
                    if not page_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Page ID is required for update_page",
                            output_handle="error"
                        )
                    
                    response = await client.patch(
                        f"https://api.notion.com/v1/pages/{page_id}",
                        headers=headers,
                        json={"properties": properties},
                    )
                    
                elif operation == "query_database":
                    if not database_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Database ID is required for query_database",
                            output_handle="error"
                        )
                    
                    payload = {}
                    if filter_obj:
                        payload["filter"] = filter_obj
                    
                    response = await client.post(
                        f"https://api.notion.com/v1/databases/{database_id}/query",
                        headers=headers,
                        json=payload,
                    )
                    
                elif operation == "create_database_item":
                    if not database_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Database ID is required for create_database_item",
                            output_handle="error"
                        )
                    
                    payload = {
                        "parent": {"database_id": database_id},
                        "properties": properties,
                    }
                    
                    response = await client.post(
                        "https://api.notion.com/v1/pages",
                        headers=headers,
                        json=payload,
                    )
                
                else:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Unknown operation: {operation}",
                        output_handle="error"
                    )
                
                if response.status_code not in (200, 201):
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("message", response.text)
                    except Exception:
                        error_msg = response.text
                    return NodeExecutionResult(
                        success=False,
                        error=f"Notion API error: {error_msg}",
                        output_handle="error"
                    )
                
                data = response.json()
                return NodeExecutionResult(
                    success=True,
                    data=data,
                    output_handle="success"
                )
                
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Notion error: {str(e)}",
                output_handle="error"
            )



class AirtableNode(BaseNodeHandler):
    """
    Interact with Airtable bases and tables.
    
    Create, read, update, and delete records.
    """
    
    node_type = "airtable"
    name = "Airtable"
    category = NodeCategory.INTEGRATION.value
    description = "Manage Airtable records"
    icon = "🗂️"
    color = "#18bfff"  # Airtable blue
    
    fields = [
        FieldConfig(
            name="credential",
            label="Airtable API Key",
            field_type=FieldType.CREDENTIAL,
            description="Select your Airtable credential"
        ),
        FieldConfig(
            name="operation",
            label="Operation",
            field_type=FieldType.SELECT,
            options=["create", "get", "update", "delete", "list"],
            default="create"
        ),
        FieldConfig(
            name="base_id",
            label="Base ID",
            field_type=FieldType.STRING,
            placeholder="appXXXXXXXXXXXXXX",
            description="Airtable base ID"
        ),
        FieldConfig(
            name="table_name",
            label="Table Name",
            field_type=FieldType.STRING,
            placeholder="My Table",
            description="Name of the table"
        ),
        FieldConfig(
            name="record_id",
            label="Record ID",
            field_type=FieldType.STRING,
            required=False,
            placeholder="recXXXXXXXXXXXXXX",
            description="Record ID (for get/update/delete)"
        ),
        FieldConfig(
            name="fields",
            label="Fields (JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={},
            description="Record fields as JSON object"
        ),
        FieldConfig(
            name="filter_formula",
            label="Filter Formula",
            field_type=FieldType.STRING,
            required=False,
            description="Airtable formula for filtering (list operation)"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        operation = config.get("operation", "create")
        base_id = config.get("base_id", "")
        table_name = config.get("table_name", "")
        record_id = config.get("record_id", "")
        fields = config.get("fields", {})
        filter_formula = config.get("filter_formula", "")
        
        if not base_id or not table_name:
            return NodeExecutionResult(
                success=False,
                error="Base ID and Table Name are required",
                output_handle="error"
            )
        
        # Get API key
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "api_key" not in creds:
            return NodeExecutionResult(
                success=False,
                error="Airtable API key not configured",
                output_handle="error"
            )
        
        api_key = creds["api_key"]
        base_url = f"https://api.airtable.com/v0/{base_id}/{quote(table_name)}"
        
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                
                if operation == "create":
                    response = await client.post(
                        base_url,
                        headers=headers,
                        json={"fields": fields},
                    )
                    
                elif operation == "get":
                    if not record_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Record ID is required for get operation",
                            output_handle="error"
                        )
                    
                    response = await client.get(
                        f"{base_url}/{record_id}",
                        headers=headers,
                    )
                    
                elif operation == "update":
                    if not record_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Record ID is required for update operation",
                            output_handle="error"
                        )
                    
                    response = await client.patch(
                        f"{base_url}/{record_id}",
                        headers=headers,
                        json={"fields": fields},
                    )
                    
                elif operation == "delete":
                    if not record_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Record ID is required for delete operation",
                            output_handle="error"
                        )
                    
                    response = await client.delete(
                        f"{base_url}/{record_id}",
                        headers=headers,
                    )
                    
                elif operation == "list":
                    params = {}
                    if filter_formula:
                        params["filterByFormula"] = filter_formula
                    
                    response = await client.get(
                        base_url,
                        headers=headers,
                        params=params,
                    )
                
                else:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Unknown operation: {operation}",
                        output_handle="error"
                    )
                
                if response.status_code not in (200, 201):
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("error", {}).get("message", response.text)
                    except Exception:
                        error_msg = response.text
                    return NodeExecutionResult(
                        success=False,
                        error=f"Airtable API error: {error_msg}",
                        output_handle="error"
                    )
                
                data = response.json()
                return NodeExecutionResult(
                    success=True,
                    data=data,
                    output_handle="success"
                )
                
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Airtable error: {str(e)}",
                output_handle="error"
            )



class TelegramNode(BaseNodeHandler):
    """
    Send messages via Telegram Bot API.
    
    Supports text, photos, documents, and more.
    """
    
    node_type = "telegram"
    name = "Telegram"
    category = NodeCategory.INTEGRATION.value
    description = "Send Telegram messages"
    icon = "✈️"
    color = "#0088cc"  # Telegram blue
    
    fields = [
        FieldConfig(
            name="credential",
            label="Bot Token",
            field_type=FieldType.CREDENTIAL,
            description="Select your Telegram bot credential"
        ),
        FieldConfig(
            name="operation",
            label="Operation",
            field_type=FieldType.SELECT,
            options=["send_message", "send_photo", "send_document"],
            default="send_message"
        ),
        FieldConfig(
            name="chat_id",
            label="Chat ID",
            field_type=FieldType.STRING,
            placeholder="123456789 or @channel_name",
            description="Telegram chat ID or username"
        ),
        FieldConfig(
            name="text",
            label="Text",
            field_type=FieldType.STRING,
            required=False,
            placeholder="Message text",
            description="Message text (for send_message)"
        ),
        FieldConfig(
            name="photo_url",
            label="Photo URL",
            field_type=FieldType.STRING,
            required=False,
            placeholder="https://example.com/image.jpg",
            description="Photo URL (for send_photo)"
        ),
        FieldConfig(
            name="document_url",
            label="Document URL",
            field_type=FieldType.STRING,
            required=False,
            placeholder="https://example.com/file.pdf",
            description="Document URL (for send_document)"
        ),
        FieldConfig(
            name="parse_mode",
            label="Parse Mode",
            field_type=FieldType.SELECT,
            options=["", "Markdown", "MarkdownV2", "HTML"],
            default="",
            required=False,
            description="Message formatting mode"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        operation = config.get("operation", "send_message")
        chat_id = config.get("chat_id", "")
        text = config.get("text", "")
        photo_url = config.get("photo_url", "")
        document_url = config.get("document_url", "")
        parse_mode = config.get("parse_mode", "")
        
        if not chat_id:
            return NodeExecutionResult(
                success=False,
                error="Chat ID is required",
                output_handle="error"
            )
        
        # Get bot token
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "bot_token" not in creds:
            return NodeExecutionResult(
                success=False,
                error="Telegram bot token not configured",
                output_handle="error"
            )
        
        bot_token = creds["bot_token"]
        base_url = f"https://api.telegram.org/bot{bot_token}"
        
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                payload = {"chat_id": chat_id}
                
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                
                if operation == "send_message":
                    if not text:
                        return NodeExecutionResult(
                            success=False,
                            error="Text is required for send_message",
                            output_handle="error"
                        )
                    payload["text"] = text
                    endpoint = f"{base_url}/sendMessage"
                    
                elif operation == "send_photo":
                    if not photo_url:
                        return NodeExecutionResult(
                            success=False,
                            error="Photo URL is required for send_photo",
                            output_handle="error"
                        )
                    payload["photo"] = photo_url
                    if text:
                        payload["caption"] = text
                    endpoint = f"{base_url}/sendPhoto"
                    
                elif operation == "send_document":
                    if not document_url:
                        return NodeExecutionResult(
                            success=False,
                            error="Document URL is required for send_document",
                            output_handle="error"
                        )
                    payload["document"] = document_url
                    if text:
                        payload["caption"] = text
                    endpoint = f"{base_url}/sendDocument"
                
                else:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Unknown operation: {operation}",
                        output_handle="error"
                    )
                
                response = await client.post(endpoint, json=payload)
                
                if response.status_code != 200:
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("description", response.text)
                    except Exception:
                        error_msg = response.text
                    return NodeExecutionResult(
                        success=False,
                        error=f"Telegram API error: {error_msg}",
                        output_handle="error"
                    )
                
                data = response.json()
                
                if not data.get("ok"):
                    return NodeExecutionResult(
                        success=False,
                        error=f"Telegram error: {data.get('description', 'Unknown error')}",
                        output_handle="error"
                    )
                
                result = data.get("result", {})
                return NodeExecutionResult(
                    success=True,
                    data={
                        "message_id": result.get("message_id"),
                        "chat_id": result.get("chat", {}).get("id"),
                        "date": result.get("date"),
                    },
                    output_handle="success"
                )
                
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Telegram error: {str(e)}",
                output_handle="error"
            )



class TrelloNode(BaseNodeHandler):
    """
    Manage Trello boards, lists, and cards.
    
    Create and update Trello cards programmatically.
    """
    
    node_type = "trello"
    name = "Trello"
    category = NodeCategory.INTEGRATION.value
    description = "Manage Trello cards and boards"
    icon = "📋"
    color = "#0079bf"  # Trello blue
    
    fields = [
        FieldConfig(
            name="credential",
            label="Trello API Credential",
            field_type=FieldType.CREDENTIAL,
            description="Select your Trello credential (API key + token)"
        ),
        FieldConfig(
            name="operation",
            label="Operation",
            field_type=FieldType.SELECT,
            options=["create_card", "update_card", "get_card", "delete_card", "get_board_lists"],
            default="create_card"
        ),
        FieldConfig(
            name="board_id",
            label="Board ID",
            field_type=FieldType.STRING,
            required=False,
            placeholder="5f8b9a2c...",
            description="Trello board ID"
        ),
        FieldConfig(
            name="list_id",
            label="List ID",
            field_type=FieldType.STRING,
            required=False,
            placeholder="5f8b9a2c...",
            description="Trello list ID (for create_card)"
        ),
        FieldConfig(
            name="card_id",
            label="Card ID",
            field_type=FieldType.STRING,
            required=False,
            placeholder="5f8b9a2c...",
            description="Trello card ID (for update/get/delete)"
        ),
        FieldConfig(
            name="name",
            label="Card Name",
            field_type=FieldType.STRING,
            required=False,
            placeholder="New Task",
            description="Card title"
        ),
        FieldConfig(
            name="description",
            label="Description",
            field_type=FieldType.STRING,
            required=False,
            description="Card description"
        ),
        FieldConfig(
            name="labels",
            label="Labels",
            field_type=FieldType.STRING,
            required=False,
            placeholder="red,green,blue",
            description="Comma-separated label colors"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        operation = config.get("operation", "create_card")
        board_id = config.get("board_id", "")
        list_id = config.get("list_id", "")
        card_id = config.get("card_id", "")
        name = config.get("name", "")
        description = config.get("description", "")
        labels = config.get("labels", "")
        
        # Get API credentials
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "api_key" not in creds or "token" not in creds:
            return NodeExecutionResult(
                success=False,
                error="Trello API credentials not configured",
                output_handle="error"
            )
        
        api_key = creds["api_key"]
        token = creds["token"]
        
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                auth_params = {"key": api_key, "token": token}
                
                if operation == "create_card":
                    if not list_id or not name:
                        return NodeExecutionResult(
                            success=False,
                            error="List ID and Name are required for create_card",
                            output_handle="error"
                        )
                    
                    params = {**auth_params, "idList": list_id, "name": name}
                    if description:
                        params["desc"] = description
                    if labels:
                        params["idLabels"] = labels
                    
                    response = await client.post(
                        "https://api.trello.com/1/cards",
                        params=params,
                    )
                    
                elif operation == "update_card":
                    if not card_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Card ID is required for update_card",
                            output_handle="error"
                        )
                    
                    params = auth_params.copy()
                    if name:
                        params["name"] = name
                    if description:
                        params["desc"] = description
                    if labels:
                        params["idLabels"] = labels
                    
                    response = await client.put(
                        f"https://api.trello.com/1/cards/{card_id}",
                        params=params,
                    )
                    
                elif operation == "get_card":
                    if not card_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Card ID is required for get_card",
                            output_handle="error"
                        )
                    
                    response = await client.get(
                        f"https://api.trello.com/1/cards/{card_id}",
                        params=auth_params,
                    )
                    
                elif operation == "delete_card":
                    if not card_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Card ID is required for delete_card",
                            output_handle="error"
                        )
                    
                    response = await client.delete(
                        f"https://api.trello.com/1/cards/{card_id}",
                        params=auth_params,
                    )
                    
                elif operation == "get_board_lists":
                    if not board_id:
                        return NodeExecutionResult(
                            success=False,
                            error="Board ID is required for get_board_lists",
                            output_handle="error"
                        )
                    
                    response = await client.get(
                        f"https://api.trello.com/1/boards/{board_id}/lists",
                        params=auth_params,
                    )
                
                else:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Unknown operation: {operation}",
                        output_handle="error"
                    )
                
                if response.status_code not in (200, 201):
                    return NodeExecutionResult(
                        success=False,
                        error=f"Trello API error: {response.text}",
                        output_handle="error"
                    )
                
                data = response.json()
                return NodeExecutionResult(
                    success=True,
                    data=data,
                    output_handle="success"
                )
                
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Trello error: {str(e)}",
                output_handle="error"
            )



class GitHubNode(BaseNodeHandler):
    """
    Interact with GitHub repositories, issues, and pull requests.
    
    Automate GitHub workflows and notifications.
    """
    
    node_type = "github"
    name = "GitHub"
    category = NodeCategory.INTEGRATION.value
    description = "Manage GitHub repositories and issues"
    icon = "🐙"
    color = "#181717"  # GitHub black
    
    fields = [
        FieldConfig(
            name="credential",
            label="GitHub Token",
            field_type=FieldType.CREDENTIAL,
            description="Select your GitHub personal access token"
        ),
        FieldConfig(
            name="operation",
            label="Operation",
            field_type=FieldType.SELECT,
            options=["create_issue", "update_issue", "get_issue", "create_pr", "get_repo"],
            default="create_issue"
        ),
        FieldConfig(
            name="owner",
            label="Repository Owner",
            field_type=FieldType.STRING,
            placeholder="octocat",
            description="GitHub username or organization"
        ),
        FieldConfig(
            name="repo",
            label="Repository Name",
            field_type=FieldType.STRING,
            placeholder="hello-world",
            description="Repository name"
        ),
        FieldConfig(
            name="issue_number",
            label="Issue Number",
            field_type=FieldType.STRING,
            required=False,
            placeholder="42",
            description="Issue number (for update/get operations)"
        ),
        FieldConfig(
            name="title",
            label="Title",
            field_type=FieldType.STRING,
            required=False,
            placeholder="Bug: Something is broken",
            description="Issue or PR title"
        ),
        FieldConfig(
            name="body",
            label="Body",
            field_type=FieldType.STRING,
            required=False,
            description="Issue or PR description (supports Markdown)"
        ),
        FieldConfig(
            name="labels",
            label="Labels",
            field_type=FieldType.JSON,
            required=False,
            default=[],
            description="Array of label names"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        operation = config.get("operation", "create_issue")
        owner = config.get("owner", "")
        repo = config.get("repo", "")
        issue_number = config.get("issue_number", "")
        title = config.get("title", "")
        body = config.get("body", "")
        labels = config.get("labels", [])
        
        if not owner or not repo:
            return NodeExecutionResult(
                success=False,
                error="Repository owner and name are required",
                output_handle="error"
            )
        
        # Get token
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "token" not in creds:
            return NodeExecutionResult(
                success=False,
                error="GitHub token not configured",
                output_handle="error"
            )
        
        token = creds["token"]
        
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                headers = {
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                }
                
                if operation == "create_issue":
                    if not title:
                        return NodeExecutionResult(
                            success=False,
                            error="Title is required for create_issue",
                            output_handle="error"
                        )
                    
                    payload = {"title": title}
                    if body:
                        payload["body"] = body
                    if labels:
                        payload["labels"] = labels
                    
                    response = await client.post(
                        f"https://api.github.com/repos/{owner}/{repo}/issues",
                        headers=headers,
                        json=payload,
                    )
                    
                elif operation == "update_issue":
                    if not issue_number:
                        return NodeExecutionResult(
                            success=False,
                            error="Issue number is required for update_issue",
                            output_handle="error"
                        )
                    
                    payload = {}
                    if title:
                        payload["title"] = title
                    if body:
                        payload["body"] = body
                    if labels:
                        payload["labels"] = labels
                    
                    response = await client.patch(
                        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
                        headers=headers,
                        json=payload,
                    )
                    
                elif operation == "get_issue":
                    if not issue_number:
                        return NodeExecutionResult(
                            success=False,
                            error="Issue number is required for get_issue",
                            output_handle="error"
                        )
                    
                    response = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
                        headers=headers,
                    )
                    
                elif operation == "get_repo":
                    response = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}",
                        headers=headers,
                    )
                
                else:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Unknown operation: {operation}",
                        output_handle="error"
                    )
                
                if response.status_code not in (200, 201):
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("message", response.text)
                    except Exception:
                        error_msg = response.text
                    return NodeExecutionResult(
                        success=False,
                        error=f"GitHub API error: {error_msg}",
                        output_handle="error"
                    )
                
                data = response.json()
                return NodeExecutionResult(
                    success=True,
                    data=data,
                    output_handle="success"
                )
                
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"GitHub error: {str(e)}",
                output_handle="error"
            )



class HTTPRequestNode(BaseNodeHandler):
    """
    Make custom HTTP requests to any API.
    
    The most flexible integration node for custom APIs.
    """
    
    node_type = "http_request"
    name = "HTTP Request"
    category = NodeCategory.INTEGRATION.value
    description = "Make custom HTTP/API requests"
    icon = "🌐"
    color = "#666666"  # Gray
    
    fields = [
        FieldConfig(
            name="method",
            label="Method",
            field_type=FieldType.SELECT,
            options=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
            default="GET"
        ),
        FieldConfig(
            name="url",
            label="URL",
            field_type=FieldType.STRING,
            placeholder="https://api.example.com/endpoint",
            description="Full API endpoint URL"
        ),
        FieldConfig(
            name="headers",
            label="Headers (JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={},
            description="HTTP headers as JSON object"
        ),
        FieldConfig(
            name="query_params",
            label="Query Parameters (JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={},
            description="URL query parameters as JSON object"
        ),
        FieldConfig(
            name="body",
            label="Body (JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={},
            description="Request body (for POST/PUT/PATCH)"
        ),
        FieldConfig(
            name="response_format",
            label="Response Format",
            field_type=FieldType.SELECT,
            options=["json", "text"],
            default="json",
            required=False,
            description="Expected response format"
        ),
        FieldConfig(
            name="timeout",
            label="Timeout (seconds)",
            field_type=FieldType.STRING,
            default="30",
            required=False,
            description="Request timeout in seconds"
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        method = config.get("method", "GET")
        url = config.get("url", "")
        headers = config.get("headers", {})
        query_params = config.get("query_params", {})
        body = config.get("body", {})
        response_format = config.get("response_format", "json")
        timeout = int(config.get("timeout", "30"))
        
        if not url:
            return NodeExecutionResult(
                success=False,
                error="URL is required",
                output_handle="error"
            )
        
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                request_kwargs = {
                    "headers": headers,
                    "params": query_params,
                }
                
                if method in ["POST", "PUT", "PATCH"] and body:
                    request_kwargs["json"] = body
                
                response = await client.request(
                    method=method,
                    url=url,
                    **request_kwargs,
                )
                
                # Parse response
                if response_format == "json":
                    try:
                        response_data = response.json()
                    except Exception:
                        response_data = {"text": response.text}
                else:
                    response_data = {"text": response.text}
                
                return NodeExecutionResult(
                    success=True,
                    data={
                        "status_code": response.status_code,
                        "headers": dict(response.headers),
                        "body": response_data,
                    },
                    output_handle="success"
                )
                
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error=f"Request timeout after {timeout} seconds",
                output_handle="error"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"HTTP request error: {str(e)}",
                output_handle="error"
            )
