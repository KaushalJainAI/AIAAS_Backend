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
    NodeItem,
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
        # User requested instant execution, removing artificial delay
        # import asyncio
        # await asyncio.sleep(0.8)
        
        return NodeExecutionResult(
            success=True,
            items=[NodeItem(json={
                "triggered_at": datetime.now().isoformat(),
                "execution_id": str(context.execution_id),
                "trigger_type": "manual"
            })],
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
        FieldConfig(
            name="test_data",
            label="Test Data (Mock JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={"body": {"test": "data"}, "headers": {}, "query": {}},
            description="Mock data to use when testing this node manually."
        ),
    ]
    static_output_fields = ["headers", "body", "query", "url", "method", "received_at"]
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
        # Fallback to test_data if input_data is empty (manual full workflow test)
        source_data = input_data
        if not input_data and "test_data" in config:
            source_data = config["test_data"]
            
        # Webhook data comes from source_data (set by webhook handler or mock)
        return NodeExecutionResult(
            success=True,
            items=[NodeItem(json={
                "received_at": datetime.now().isoformat(),
                "method": config.get("method", "POST"),
                "path": config.get("path", ""),
                "headers": source_data.get("headers", {}),
                "body": source_data.get("body", {}),
                "query": source_data.get("query", {}),
                "url": source_data.get("url", ""),
            })],
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
            items=[NodeItem(json=data)],
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
        FieldConfig(
            name="test_data",
            label="Test Data (Mock JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={"from": "tester@example.com", "subject": "Test Email", "body": "Hello world"},
            description="Mock data to use when testing this node manually."
        ),
    ]
    static_output_fields = ["from", "to", "subject", "body", "html_body", "attachments", "date", "message_id", "triggered_at"]
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
            items=[NodeItem(json={
                "triggered_at": datetime.now().isoformat(),
                "from": input_data.get("from", ""),
                "to": input_data.get("to", ""),
                "subject": input_data.get("subject", ""),
                "body": input_data.get("body", ""),
                "html_body": input_data.get("html_body", ""),
                "attachments": input_data.get("attachments", []),
                "date": input_data.get("date", ""),
                "message_id": input_data.get("message_id", ""),
            })],
            output_handle="output-0"
        )

    async def poll(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        context: 'ExecutionContext'
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Poll IMAP for new emails since last seen UID.
        """
        import imaplib
        import email
        from email.header import decode_header
        
        credential = context.credentials.get(config.get("credential")) if context.credentials else None
        if not credential:
            return [], state

        host = credential.get("host")
        user = credential.get("user")
        password = credential.get("password")
        port = int(credential.get("port", 993))
        mailbox = config.get("mailbox", "INBOX")

        if not all([host, user, password]):
            return [], state

        try:
            # Use sync imaplib in a thread or just run it (Celery allows sync)
            # For simplicity and reliability in Celery workers:
            mail = imaplib.IMAP4_SSL(host, port)
            mail.login(user, password)
            mail.select(mailbox)

            # Get current UIDVALIDITY to ensure mailbox hasn't been recreated
            resp, data = mail.status(mailbox, '(UIDVALIDITY)')
            current_validity = data[0].decode().split('UIDVALIDITY ')[1].rstrip(')') if resp == 'OK' else None
            
            last_validity = state.get("uid_validity")
            last_uid = state.get("last_uid", 0)

            # Reset cursor if mailbox changed
            if current_validity != last_validity:
                last_uid = 0
                last_validity = current_validity

            # Search for emails with UID > last_uid
            search_crit = f"UID {last_uid + 1}:*"
            resp, data = mail.uid('search', None, search_crit)
            
            new_items = []
            max_uid = last_uid

            if resp == 'OK' and data[0]:
                uids = data[0].split()
                for uid_bytes in uids:
                    uid = int(uid_bytes)
                    if uid <= last_uid: continue
                    max_uid = max(max_uid, uid)

                    # Fetch email
                    resp, msg_data = mail.uid('fetch', uid_bytes, '(RFC822)')
                    if resp != 'OK': continue
                    
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    # Basic parsing
                    subject = decode_header(msg.get("Subject", ""))[0][0]
                    if isinstance(subject, bytes): subject = subject.decode()
                    
                    sender = msg.get("From", "")
                    
                    # Filtering
                    if config.get("filter_sender") and config.get("filter_sender") not in sender:
                        continue
                    if config.get("filter_subject") and config.get("filter_subject").lower() not in subject.lower():
                        continue

                    new_items.append({
                        "from": sender,
                        "subject": subject,
                        "date": msg.get("Date", ""),
                        "message_id": msg.get("Message-ID", ""),
                        "uid": uid
                    })

            mail.logout()
            return new_items, {"last_uid": max_uid, "uid_validity": last_validity}

        except Exception as e:
            print(f"Email Poll error: {e}")
            return [], state



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
        FieldConfig(
            name="test_data",
            label="Test Data (Mock JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={"form_data": {"name": "Test User", "email": "test@example.com"}},
            description="Mock data to use when testing this node manually."
        ),
    ]
    static_output_fields = ["form_data", "submitted_at", "submitter_ip", "user_agent"]
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
            items=[NodeItem(json={
                "submitted_at": datetime.now().isoformat(),
                "form_data": input_data.get("form_data", {}),
                "submitter_ip": input_data.get("ip_address", ""),
                "user_agent": input_data.get("user_agent", ""),
            })],
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
        FieldConfig(
            name="test_data",
            label="Test Data (Mock JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={"text": "Hello Slack", "user": "U12345", "channel": "C12345"},
            description="Mock data to use when testing this node manually."
        ),
    ]
    static_output_fields = ["channel", "user", "text", "timestamp", "thread_ts", "event_type", "triggered_at"]
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
            items=[NodeItem(json={
                "triggered_at": datetime.now().isoformat(),
                "event_type": config.get("event_type", "message"),
                "channel": input_data.get("channel", ""),
                "user": input_data.get("user", ""),
                "text": input_data.get("text", ""),
                "timestamp": input_data.get("ts", ""),
                "thread_ts": input_data.get("thread_ts", ""),
                "event_data": input_data,
            })],
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
            items=[NodeItem(json={
                "triggered_at": datetime.now().isoformat(),
                "spreadsheet_id": config.get("spreadsheet_id", ""),
                "sheet_name": config.get("sheet_name", "Sheet1"),
                "trigger_type": config.get("trigger_on", "new_row"),
                "row_data": input_data.get("row_data", {}),
                "change_type": input_data.get("change_type", ""),
            })],
            output_handle="output-0"
        )

    async def poll(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        context: 'ExecutionContext'
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Poll Google Sheets for new rows based on row index.
        """
        # Placeholder for Google API client logic
        # In a real implementation, this would use the token from context.credentials
        spreadsheet_id = config.get("spreadsheet_id")
        sheet_name = config.get("sheet_name", "Sheet1")
        if not spreadsheet_id:
            return [], state

        last_row = state.get("last_row", 0)
        
        try:
            # This is where the Google Sheets API call would go
            # Example logic using a hypothetical helper or direct HTTP
            # For now, we simulate the logic of finding 'new rows'
            
            # 1. Fetch current max row count (or all rows)
            # 2. Filter for index > last_row
            # 3. Return those as new_items
            
            # Since we can't actually call Google here, we'll provide the structural logic
            new_items = []
            current_max_row = last_row # Simulation
            
            # Logic:
            # resp = await google_client.get(f"{spreadsheet_id}/values/{sheet_name}!A{last_row+1}:ZZ")
            # rows = resp.get('values', [])
            # for i, row in enumerate(rows):
            #     new_items.append({"row_number": last_row + i + 1, "row_data": row})
            # current_max_row = last_row + len(rows)

            return new_items, {"last_row": current_max_row}
            
        except Exception as e:
            print(f"Google Sheets Poll error: {e}")
            return [], state



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
        FieldConfig(
            name="include_raw",
            label="Include Raw Payload",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="Include the full raw GitHub payload in the output"
        ),
        FieldConfig(
            name="test_data",
            label="Test Data (Mock JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={"repository": "owner/repo", "sender": "octocat"},
            description="Mock data to use when testing this node manually."
        ),
    ]
    static_output_fields = ["project_context", "change_summary", "code_changes", "repository", "event", "sender", "triggered_at"]

    inputs = []
    outputs = [HandleDef(id="output-0", label="On event")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Fallback to test_data if input_data is empty
        source_data = input_data
        if not input_data and "test_data" in config:
            source_data = config["test_data"]

        # Extract raw data from the webhook input
        payload = source_data.get("payload", {})
        event_type = source_data.get("event", "unknown")
        
        # Normalize repo and branch info
        repo_full_name = payload.get('repository', {}).get('full_name', config.get("repository", ""))
        branch = payload.get('ref', '').split('/')[-1] if payload.get('ref') else None

        # Process commits for code changes
        commits = [c for c in payload.get('commits', []) if c.get('distinct')]
        diff_entries = []
        total_adds = 0
        total_dels = 0

        # Try to fetch actual patches if we have repo and commits
        before_sha = payload.get('before')
        after_sha = payload.get('after')
        
        # We need a token to fetch private repo details or to avoid rate limits
        credential_id = config.get("credential")
        creds = await context.get_credential(credential_id) if credential_id else None
        # Try to find any github credential if not specified
        if not creds:
            for c_id, c_data in context.credentials.items():
                if isinstance(c_data, dict) and "token" in c_data:
                    creds = c_data
                    break
        
        token = creds.get("token") if creds else None
        
        if repo_full_name and after_sha:
            try:
                # Use GitHub API to fetch the actual diff
                headers = {"Accept": "application/vnd.github.v3+json"}
                if token:
                    headers["Authorization"] = f"token {token}"
                
                async with httpx.AsyncClient(timeout=20) as client:
                    if before_sha and before_sha != "0000000000000000000000000000000000000000":
                        # Multi-commit push: use compare API
                        url = f"https://api.github.com/repos/{repo_full_name}/compare/{before_sha}...{after_sha}"
                    else:
                        # First push or single commit: use commit API
                        url = f"https://api.github.com/repos/{repo_full_name}/commits/{after_sha}"
                    
                    response = await client.get(url, headers=headers)
                    if response.status_code == 200:
                        api_data = response.json()
                        files = api_data.get('files', [])
                        
                        # Extract stats from API data
                        stats = api_data.get('stats', {})
                        total_adds = stats.get('additions', 0)
                        total_dels = stats.get('deletions', 0)

                        for file in files:
                            diff_entries.append({
                                "file": file.get('filename'),
                                "status": file.get('status'),
                                "patch": file.get('patch', '')  # This is the actual code change!
                            })
            except Exception as e:
                # Log error but continue with whatever data we HAVE from the payload
                import logging
                logging.getLogger(__name__).warning(f"Failed to fetch GitHub diff data: {e}")

        # If API failed or was skipped, fallback to payload summaries if present (rare)
        if not diff_entries:
            for commit in commits:
                total_adds += commit.get('stats', {}).get('additions', 0)
                total_dels += commit.get('stats', {}).get('deletions', 0)
                for file in commit.get('files', []): # Some webhooks might have this if configured
                    diff_entries.append({
                        "file": file.get('filename'),
                        "status": file.get('status'),
                        "patch": file.get('patch', '')
                    })

        # Build the normalized output for the AI node
        normalized_data = {
            # New refined structure
            "project_context": {
                "repository": repo_full_name,
                "branch": branch,
                "head_sha": payload.get('after'),
                "sender": payload.get('sender', {}).get('login')
            },
            "change_summary": {
                "commit_count": len(commits),
                "total_additions": total_adds,
                "total_deletions": total_dels,
                "messages": [c.get('message') for c in commits]
            },
            "code_changes": diff_entries,
            "raw_payload": payload if config.get("include_raw", False) else {},
            
            # Backward compatibility keys
            "triggered_at": datetime.now().isoformat(),
            "repository": repo_full_name,
            "event": event_type,
            "action": input_data.get("action", ""),
            "sender": payload.get('sender', {}),
            "ref": input_data.get("ref", ""),
            "payload": payload,
        }

        return NodeExecutionResult(
            success=True,
            items=[NodeItem(json=normalized_data)],
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
        FieldConfig(
            name="test_data",
            label="Test Data (Mock JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={"content": "Hello Discord", "author": {"username": "tester"}},
            description="Mock data to use when testing this node manually."
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
        # Fallback to test_data if input_data is empty
        actual_input = input_data
        if not input_data and "test_data" in config:
            actual_input = config["test_data"]

        # Discord event data comes from actual_input
        return NodeExecutionResult(
            success=True,
            items=[NodeItem(json={
                "triggered_at": datetime.now().isoformat(),
                "event_type": config.get("event_type", "message"),
                "channel_id": actual_input.get("channel_id", ""),
                "guild_id": actual_input.get("guild_id", ""),
                "author": actual_input.get("author", {}),
                "content": actual_input.get("content", ""),
                "timestamp": actual_input.get("timestamp", ""),
                "event_data": actual_input,
            })],
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
        FieldConfig(
            name="secret_token",
            label="Secret Token",
            field_type=FieldType.STRING,
            required=False,
            placeholder="my-secret-token",
            description="Optional secret token for webhook verification (stored in X-Telegram-Bot-Api-Secret-Token)"
        ),
        FieldConfig(
            name="test_data",
            label="Test Data (Mock JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={"text": "Hello Telegram", "from": {"id": 1234, "username": "tester"}},
            description="Mock data to use when testing this node manually."
        ),
    ]
    static_output_fields = ["chat_id", "text", "command", "args", "user", "chat", "message", "trigger_type", "triggered_at"]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On update")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Fallback to test_data if input_data is empty
        actual_input = input_data
        if not input_data and "test_data" in config:
            actual_input = config["test_data"]

        # Extract data from payload (webhooks) or direct actual_input (polling)
        payload = actual_input.get("payload", actual_input)
        
        message = payload.get("message", {})
        # Handle callback queries or edited messages if present
        if not message:
            message = payload.get("edited_message", {})
        if not message and "callback_query" in payload:
            message = payload["callback_query"].get("message", {})
            
        # Fallback for flat manual test_data
        if not message and "text" in payload:
            message = payload
            
        chat = message.get("chat", {})
        user = message.get("from", {})
        # For direct message updates, 'from' is at top level of message
        # For callbacks, user is 'from' in callback_query
        if "callback_query" in payload:
            user = payload["callback_query"].get("from", {})
            
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
            items=[NodeItem(json={
                "triggered_at": datetime.now().isoformat(),
                "update_id": payload.get("update_id", ""),
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
                "raw_update": payload,
                "trigger_type": config.get("trigger_on", "message"),
            })],
            output_handle="output-0"
        )

    async def poll(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        context: 'ExecutionContext'
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Poll Telegram for new updates using offset.
        """
        credential = context.credentials.get(config.get("credential")) if context.credentials else None
        token = credential.get("bot_token") if credential else None
        
        if not token:
            return [], state

        offset = state.get("offset", 0)
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        params = {"offset": offset, "timeout": 10}
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                
            data = response.json()
            if not data.get("ok"):
                return [], state
                
            updates = data.get("result", [])
            new_items = []
            max_update_id = offset - 1
            
            for update in updates:
                update_id = update.get("update_id")
                max_update_id = max(max_update_id, update_id)
                
                # Basic filtering by update type if needed
                trigger_on = config.get("trigger_on", "message")
                
                is_match = False
                if trigger_on == "message" and "message" in update: is_match = True
                elif trigger_on == "edited_message" and "edited_message" in update: is_match = True
                elif trigger_on == "callback_query" and "callback_query" in update: is_match = True
                elif trigger_on == "command":
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    target_cmd = config.get("command", "")
                    if text.startswith(f"/{target_cmd}"):
                        is_match = True
                
                if is_match:
                    new_items.append(update)

            # Update offset for next poll
            new_offset = max_update_id + 1
            return new_items, {"offset": new_offset}
            
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Telegram Poll error: {e}")
            return [], state



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
            items=[NodeItem(json={
                "triggered_at": datetime.now().isoformat(),
                "feed_url": config.get("feed_url", ""),
                "title": input_data.get("title", ""),
                "link": input_data.get("link", ""),
                "description": input_data.get("description", ""),
                "published": input_data.get("published", ""),
                "author": input_data.get("author", ""),
                "content": input_data.get("content", ""),
            })],
            output_handle="output-0"
        )

    async def poll(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        context: 'ExecutionContext'
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Poll RSS feed for new items using sliding window for GUIDs.
        """
        import xml.etree.ElementTree as ET
        feed_url = config.get("feed_url")
        if not feed_url:
            return [], state

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(feed_url)
                response.raise_for_status()
                
            root = ET.fromstring(response.content)
            items = []
            
            # Identify items (RSS 2.0 or Atom)
            if root.tag.endswith('rss'):
                raw_items = root.findall('.//item')
            elif root.tag.endswith('feed'):
                raw_items = root.findall('{http://www.w3.org/2005/Atom}entry')
            else:
                raw_items = []

            seen_guids = state.get("seen_guids", [])
            window_size = state.get("window_size", 100)
            new_items = []
            
            for item in raw_items:
                # Get unique ID
                guid = None
                if root.tag.endswith('rss'):
                    guid_el = item.find('guid')
                    guid = guid_el.text if guid_el is not None else item.find('link').text
                    title = item.find('title').text if item.find('title') is not None else ""
                    link = item.find('link').text if item.find('link') is not None else ""
                else:
                    guid_el = item.find('{http://www.w3.org/2005/Atom}id')
                    guid = guid_el.text if guid_el is not None else item.find('{http://www.w3.org/2005/Atom}link').get('href')
                    title = item.find('{http://www.w3.org/2005/Atom}title').text if item.find('{http://www.w3.org/2005/Atom}title') is not None else ""
                    link_el = item.find('{http://www.w3.org/2005/Atom}link')
                    link = link_el.get('href') if link_el is not None else ""

                if guid and guid not in seen_guids:
                    # Apply Title Filter if present
                    title_filter = config.get("title_filter")
                    if title_filter and title_filter.lower() not in title.lower():
                        continue
                        
                    new_items.append({
                        "title": title,
                        "link": link,
                        "guid": guid,
                        # Add other fields as needed
                    })
                    seen_guids.append(guid)

            # Apply Sliding Window: Keep only the last N GUIDs
            if len(seen_guids) > window_size:
                seen_guids = seen_guids[-window_size:]

            return new_items, {"seen_guids": seen_guids, "window_size": window_size}
            
        except Exception as e:
            # We don't want a single feed error to crash the poller
            import logging
            logging.getLogger(__name__).error(f"RSS Poll error for {feed_url}: {e}")
            return [], state



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
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="Watch subdirectories recursively"
        ),
        FieldConfig(
            name="test_data",
            label="Test Data (Mock JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={"file_name": "test.txt", "file_path": "/tmp/test.txt", "event_type": "created"},
            description="Mock data to use when testing this node manually."
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On file change")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Fallback to test_data if input_data is empty
        actual_input = input_data
        if not input_data and "test_data" in config:
            actual_input = config["test_data"]

        # File event data comes from actual_input (set by file watcher or mock)
        return NodeExecutionResult(
            success=True,
            items=[NodeItem(json={
                "triggered_at": datetime.now().isoformat(),
                "event_type": actual_input.get("event_type", ""),
                "file_path": actual_input.get("file_path", ""),
                "file_name": actual_input.get("file_name", ""),
                "file_size": actual_input.get("file_size", 0),
                "modified_time": actual_input.get("modified_time", ""),
                "file_extension": actual_input.get("file_extension", ""),
            })],
            output_handle="output-0"
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
            credential_type="aws",
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
            field_type=FieldType.BOOLEAN,
            default=True,
            required=False,
            description="Automatically delete message after successful processing"
        ),
        FieldConfig(
            name="test_data",
            label="Test Data (Mock JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={"body": "Hello SQS", "message_id": "test-msg-123"},
            description="Mock data to use when testing this node manually."
        ),
    ]
    inputs = []
    outputs = [HandleDef(id="output-0", label="On message")]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # Fallback to test_data if input_data is empty
        actual_input = input_data
        if not input_data and "test_data" in config:
            actual_input = config["test_data"]

        # SQS message data comes from actual_input (set by SQS poller or mock)
        return NodeExecutionResult(
            success=True,
            items=[NodeItem(json={
                "triggered_at": datetime.now().isoformat(),
                "message_id": actual_input.get("message_id", ""),
                "receipt_handle": actual_input.get("receipt_handle", ""),
                "body": actual_input.get("body", ""),
                "attributes": actual_input.get("attributes", {}),
                "message_attributes": actual_input.get("message_attributes", {}),
                "sent_timestamp": actual_input.get("sent_timestamp", ""),
            })],
            output_handle="output-0"
        )
