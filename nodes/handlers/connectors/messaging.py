"""Messaging connectors: Teams, Twilio SMS, WhatsApp, Mattermost."""
from __future__ import annotations

import base64
from typing import Any

from ..base import FieldConfig, FieldType
from ..rest_base import ConnectorError, RestConnectorNode



class MicrosoftTeamsNode(RestConnectorNode):
    """Post to a Teams channel via an Incoming Webhook."""

    node_type = "microsoft_teams"
    name = "Microsoft Teams"
    description = "Post messages and cards to a Teams channel"
    icon = "💬"
    color = "#6264a7"

    credential_slug = "microsoft-teams"
    credential_key = "webhookUrl"
    # The webhook URL is itself the secret, so there is no Authorization header
    # to add — auth is possession of the URL.
    auth_style = "none"

    fields = [
        FieldConfig(
            name="credential", label="Teams Webhook", field_type=FieldType.CREDENTIAL,
            credential_type="microsoft-teams",
            description="Incoming Webhook URL for the target channel",
        ),
        FieldConfig(
            name="operation", label="Operation", field_type=FieldType.SELECT,
            options=["send_message", "send_card"], default="send_message",
        ),
        FieldConfig(name="text", label="Message", field_type=FieldType.STRING,
                    placeholder="Deployment finished"),
        FieldConfig(name="title", label="Card Title", field_type=FieldType.STRING,
                    required=False, description="Used by send_card"),
        FieldConfig(name="theme_color", label="Theme Colour", field_type=FieldType.STRING,
                    required=False, default="0076D7", description="Hex, no leading #"),
    ]
    static_output_fields = ["delivered", "operation"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) if config.get("credential") else None
        webhook_url = (creds or {}).get("webhookUrl") or (creds or {}).get("url")
        if not webhook_url:
            raise ConnectorError("Teams credential has no webhook URL.")

        text = config.get("text", "")
        if not text and operation == "send_message":
            raise ConnectorError("Message text is required.")

        if operation == "send_card":
            payload: dict[str, Any] = {
                "@type": "MessageCard",
                "@context": "https://schema.org/extensions",
                "themeColor": config.get("theme_color") or "0076D7",
                "summary": config.get("title") or "Notification",
                "sections": [{
                    "activityTitle": config.get("title") or "Notification",
                    "text": text,
                }],
            }
        else:
            payload = {"text": text}

        # Teams webhooks answer "1" as a bare body rather than JSON.
        await self.call("POST", str(webhook_url).strip(), json_body=payload)
        return {"delivered": True, "operation": operation}


class TwilioSMSNode(RestConnectorNode):
    """Send SMS and WhatsApp messages through Twilio."""

    node_type = "twilio_sms"
    name = "Twilio SMS"
    description = "Send SMS or WhatsApp messages via Twilio"
    icon = "📱"
    color = "#f22f46"

    credential_slug = "twilio"
    auth_style = "basic"
    base_url = "https://api.twilio.com/2010-04-01"

    fields = [
        FieldConfig(name="credential", label="Twilio Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="twilio"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["send_sms", "send_whatsapp", "get_message"], default="send_sms"),
        FieldConfig(name="to", label="To", field_type=FieldType.STRING,
                    placeholder="+919876543210"),
        FieldConfig(name="from_number", label="From", field_type=FieldType.STRING,
                    required=False, description="Defaults to the number on the credential"),
        FieldConfig(name="body", label="Message", field_type=FieldType.STRING),
        FieldConfig(name="message_sid", label="Message SID", field_type=FieldType.STRING,
                    required=False, description="Used by get_message"),
    ]
    static_output_fields = ["sid", "status", "to", "from"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential"))
        if not creds:
            raise ConnectorError("Twilio credential could not be loaded.")

        account_sid = (creds.get("accountSid") or creds.get("account_sid") or "").strip()
        auth_token = (creds.get("authToken") or creds.get("auth_token") or creds.get("apiKey") or "").strip()
        if not account_sid or not auth_token:
            raise ConnectorError("Twilio credential needs both an Account SID and an Auth Token.")

        # Twilio uses HTTP Basic with the SID as the username.
        basic = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()

        if operation == "get_message":
            sid = config.get("message_sid", "").strip()
            if not sid:
                raise ConnectorError("Message SID is required for get_message.")
            return await self.call(
                "GET", f"/Accounts/{account_sid}/Messages/{sid}.json", secret=basic,
            )

        to = config.get("to", "").strip()
        body = config.get("body", "")
        if not to or not body:
            raise ConnectorError("Both 'To' and 'Message' are required.")

        from_number = (config.get("from_number") or creds.get("fromNumber")
                       or creds.get("from_number") or "").strip()
        if not from_number:
            raise ConnectorError("No 'From' number given and none set on the credential.")

        if operation == "send_whatsapp":
            # Twilio distinguishes the channel by an address prefix, and silently
            # sends an ordinary SMS if it is missing.
            to = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
            from_number = from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}"

        # This endpoint is form-encoded, not JSON.
        return await self.call(
            "POST", f"/Accounts/{account_sid}/Messages.json", secret=basic,
            data={"To": to, "From": from_number, "Body": body},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )


class WhatsAppNode(RestConnectorNode):
    """Send WhatsApp messages through the Meta Cloud API."""

    node_type = "whatsapp"
    name = "WhatsApp"
    description = "Send WhatsApp messages via the Meta Cloud API"
    icon = "🟢"
    color = "#25d366"

    credential_slug = "whatsapp-cloud"
    credential_key = "accessToken"
    auth_style = "bearer"
    base_url = "https://graph.facebook.com/v21.0"

    fields = [
        FieldConfig(name="credential", label="WhatsApp Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="whatsapp-cloud"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["send_text", "send_template"], default="send_text"),
        FieldConfig(name="to", label="Recipient", field_type=FieldType.STRING,
                    placeholder="919876543210 (country code, no +)"),
        FieldConfig(name="text", label="Message", field_type=FieldType.STRING, required=False),
        FieldConfig(name="template_name", label="Template Name", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="language_code", label="Template Language", field_type=FieldType.STRING,
                    required=False, default="en_US"),
    ]
    static_output_fields = ["message_id", "to"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        phone_number_id = (creds.get("phoneNumberId") or creds.get("phone_number_id") or "").strip()
        if not phone_number_id:
            raise ConnectorError("WhatsApp credential has no Phone Number ID.")

        to = config.get("to", "").strip().lstrip("+")
        if not to:
            raise ConnectorError("Recipient is required.")

        if operation == "send_template":
            template = config.get("template_name", "").strip()
            if not template:
                raise ConnectorError("Template name is required for send_template.")
            payload: dict[str, Any] = {
                "messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": config.get("language_code") or "en_US"},
                },
            }
        else:
            text = config.get("text", "")
            if not text:
                raise ConnectorError("Message text is required.")
            payload = {
                "messaging_product": "whatsapp", "to": to,
                "type": "text", "text": {"body": text},
            }

        data = await self.call("POST", f"/{phone_number_id}/messages", secret=secret,
                               json_body=payload)
        messages = (data or {}).get("messages") or [{}]
        return {"message_id": messages[0].get("id"), "to": to, "operation": operation}


class MattermostNode(RestConnectorNode):
    """Post messages to Mattermost."""

    node_type = "mattermost"
    name = "Mattermost"
    description = "Post messages to a Mattermost channel"
    icon = "🔷"
    color = "#0058cc"

    credential_slug = "mattermost"
    credential_key = "accessToken"
    auth_style = "bearer"

    fields = [
        FieldConfig(name="credential", label="Mattermost Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="mattermost"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["post_message", "create_channel"], default="post_message"),
        FieldConfig(name="channel_id", label="Channel ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="message", label="Message", field_type=FieldType.STRING, required=False),
        FieldConfig(name="team_id", label="Team ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="channel_name", label="New Channel Name", field_type=FieldType.STRING,
                    required=False),
    ]
    static_output_fields = ["id", "channel_id", "create_at"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        server = (creds.get("serverUrl") or creds.get("server_url") or "").strip().rstrip("/")
        if not server:
            raise ConnectorError("Mattermost credential has no server URL.")

        if operation == "create_channel":
            team_id = config.get("team_id", "").strip()
            channel_name = config.get("channel_name", "").strip()
            if not team_id or not channel_name:
                raise ConnectorError("Team ID and channel name are required.")
            return await self.call(
                "POST", f"{server}/api/v4/channels", secret=secret,
                json_body={
                    "team_id": team_id,
                    "name": channel_name.lower().replace(" ", "-"),
                    "display_name": channel_name,
                    "type": "O",
                },
            )

        channel_id = config.get("channel_id", "").strip()
        message = config.get("message", "")
        if not channel_id or not message:
            raise ConnectorError("Channel ID and message are required.")
        return await self.call(
            "POST", f"{server}/api/v4/posts", secret=secret,
            json_body={"channel_id": channel_id, "message": message},
        )
