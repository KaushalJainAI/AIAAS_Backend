"""
Integration Node Handlers

Third-party service integration nodes for Gmail, Slack, and Google Sheets.
"""
import httpx
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
                message.attach(MIMEText(body, "plain"))
                message.attach(MIMEText(html_body, "html"))
            else:
                message = MIMEText(body, "plain")
            
            message["to"] = to
            message["subject"] = subject
            
            # Encode message
            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
            
            async with httpx.AsyncClient(timeout=30) as client:
                if operation == "send_email":
                    endpoint = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
                else:  # create_draft
                    endpoint = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
                
                payload = {"raw": raw_message}
                if operation == "create_draft":
                    payload = {"message": payload}
                
                response = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                
                if response.status_code not in (200, 201):
                    error_data = response.json()
                    error_msg = error_data.get("error", {}).get("message", response.text)
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
                        "Content-Type": "application/json",
                    },
                    json=payload,
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
        
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                }
                
                if operation == "read_range":
                    response = await client.get(
                        f"{base_url}/values/{range_notation}",
                        headers=headers,
                    )
                    
                    if response.status_code != 200:
                        error_data = response.json()
                        error_msg = error_data.get("error", {}).get("message", response.text)
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
                        f"{base_url}/values/{range_notation}",
                        headers=headers,
                        params={"valueInputOption": value_input_option},
                        json={"values": values},
                    )
                    
                    if response.status_code != 200:
                        error_data = response.json()
                        error_msg = error_data.get("error", {}).get("message", response.text)
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
                        f"{base_url}/values/{range_notation}:append",
                        headers=headers,
                        params={
                            "valueInputOption": value_input_option,
                            "insertDataOption": "INSERT_ROWS",
                        },
                        json={"values": values},
                    )
                    
                    if response.status_code != 200:
                        error_data = response.json()
                        error_msg = error_data.get("error", {}).get("message", response.text)
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
                        f"{base_url}/values/{range_notation}:clear",
                        headers=headers,
                    )
                    
                    if response.status_code != 200:
                        error_data = response.json()
                        error_msg = error_data.get("error", {}).get("message", response.text)
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
