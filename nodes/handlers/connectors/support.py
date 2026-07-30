"""Customer-support connectors: Zendesk, Intercom."""
from __future__ import annotations

import base64
from typing import Any

from ..base import FieldConfig, FieldType
from ..rest_base import ConnectorError, RestConnectorNode



class ZendeskNode(RestConnectorNode):
    """Tickets in Zendesk Support."""

    node_type = "zendesk"
    name = "Zendesk"
    description = "Create, update, search and comment on Zendesk tickets"
    icon = "🎫"
    color = "#03363d"

    credential_slug = "zendesk"
    auth_style = "basic"

    fields = [
        FieldConfig(name="credential", label="Zendesk Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="zendesk"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_ticket", "update_ticket", "get_ticket",
                             "search_tickets", "add_comment"],
                    default="create_ticket"),
        FieldConfig(name="subject", label="Subject", field_type=FieldType.STRING, required=False),
        FieldConfig(name="description", label="Description", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="ticket_id", label="Ticket ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="requester_email", label="Requester Email", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="status", label="Status", field_type=FieldType.SELECT,
                    options=["new", "open", "pending", "hold", "solved", "closed"],
                    required=False),
        FieldConfig(name="priority", label="Priority", field_type=FieldType.SELECT,
                    options=["low", "normal", "high", "urgent"], required=False),
        FieldConfig(name="comment", label="Comment", field_type=FieldType.STRING, required=False),
        FieldConfig(name="public", label="Public Comment", field_type=FieldType.BOOLEAN,
                    required=False, default=True),
        FieldConfig(name="query", label="Search Query", field_type=FieldType.STRING, required=False),
    ]
    static_output_fields = ["id", "subject", "status", "priority", "url"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        subdomain = (creds.get("subdomain") or "").strip()
        email = (creds.get("email") or "").strip()
        token = (creds.get("apiToken") or creds.get("api_token") or creds.get("apiKey") or "").strip()
        if not subdomain or not email or not token:
            raise ConnectorError("Zendesk credential needs a subdomain, email and API token.")

        # Zendesk API tokens authenticate as "<email>/token:<token>" — without the
        # "/token" suffix it treats the value as a password and rejects it.
        basic = base64.b64encode(f"{email}/token:{token}".encode()).decode()
        base = f"https://{subdomain}.zendesk.com/api/v2"

        if operation == "create_ticket":
            subject = config.get("subject", "").strip()
            description = config.get("description", "").strip()
            if not subject or not description:
                raise ConnectorError("Subject and description are required.")
            ticket: dict[str, Any] = {
                "subject": subject,
                "comment": {"body": description},
            }
            if config.get("priority"):
                ticket["priority"] = config["priority"]
            if config.get("status"):
                ticket["status"] = config["status"]
            if config.get("requester_email"):
                ticket["requester"] = {"email": config["requester_email"]}
            data = await self.call("POST", f"{base}/tickets.json", secret=basic,
                                   json_body={"ticket": ticket})
            return (data or {}).get("ticket", {})

        if operation == "get_ticket":
            ticket_id = config.get("ticket_id", "").strip()
            if not ticket_id:
                raise ConnectorError("Ticket ID is required.")
            data = await self.call("GET", f"{base}/tickets/{ticket_id}.json", secret=basic)
            return (data or {}).get("ticket", {})

        if operation in ("update_ticket", "add_comment"):
            ticket_id = config.get("ticket_id", "").strip()
            if not ticket_id:
                raise ConnectorError("Ticket ID is required.")
            ticket = {}
            if operation == "add_comment":
                comment = config.get("comment", "").strip()
                if not comment:
                    raise ConnectorError("Comment is required.")
                ticket["comment"] = {
                    "body": comment,
                    # Defaults to public; an internal note that posts to the
                    # customer is not a recoverable mistake.
                    "public": bool(config.get("public", True)),
                }
            else:
                for key in ("status", "priority", "subject"):
                    if config.get(key):
                        ticket[key] = config[key]
                if not ticket:
                    raise ConnectorError("Nothing to update.")
            data = await self.call("PUT", f"{base}/tickets/{ticket_id}.json", secret=basic,
                                   json_body={"ticket": ticket})
            return (data or {}).get("ticket", {})

        if operation == "search_tickets":
            query = config.get("query", "").strip()
            if not query:
                raise ConnectorError("A search query is required.")
            data = await self.call("GET", f"{base}/search.json", secret=basic,
                                   params={"query": f"type:ticket {query}"})
            return (data or {}).get("results", [])

        raise NotImplementedError(operation)


class IntercomNode(RestConnectorNode):
    """Contacts and conversations in Intercom."""

    node_type = "intercom"
    name = "Intercom"
    description = "Manage Intercom contacts and conversations"
    icon = "💬"
    color = "#1f8ded"

    credential_slug = "intercom"
    credential_key = "accessToken"
    auth_style = "bearer"
    base_url = "https://api.intercom.io"

    fields = [
        FieldConfig(name="credential", label="Intercom Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="intercom"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_contact", "search_contacts", "get_contact",
                             "list_conversations", "reply_conversation"],
                    default="create_contact"),
        FieldConfig(name="email", label="Email", field_type=FieldType.STRING, required=False),
        FieldConfig(name="name", label="Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="contact_id", label="Contact ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="conversation_id", label="Conversation ID", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="message", label="Reply Body", field_type=FieldType.STRING, required=False),
        FieldConfig(name="admin_id", label="Admin ID", field_type=FieldType.STRING, required=False,
                    description="Who the reply comes from"),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=25),
    ]
    static_output_fields = ["id", "type", "email", "created_at"]

    async def run_operation(self, operation, config, secret, context):
        # Intercom pins behaviour to an API version header; without it the
        # account's default applies and response shapes change under us.
        headers = {"Intercom-Version": "2.11"}
        limit = min(int(config.get("limit") or 25), 150)

        if operation == "create_contact":
            email = config.get("email", "").strip()
            if not email:
                raise ConnectorError("Email is required.")
            body: dict[str, Any] = {"role": "user", "email": email}
            if config.get("name"):
                body["name"] = config["name"]
            return await self.call("POST", "/contacts", secret=secret,
                                   headers=headers, json_body=body)

        if operation == "get_contact":
            contact_id = config.get("contact_id", "").strip()
            if not contact_id:
                raise ConnectorError("Contact ID is required.")
            return await self.call("GET", f"/contacts/{contact_id}", secret=secret,
                                   headers=headers)

        if operation == "search_contacts":
            email = config.get("email", "").strip()
            if not email:
                raise ConnectorError("An email to search for is required.")
            data = await self.call(
                "POST", "/contacts/search", secret=secret, headers=headers,
                json_body={"query": {"field": "email", "operator": "=", "value": email}},
            )
            return (data or {}).get("data", [])

        if operation == "list_conversations":
            data = await self.call("GET", "/conversations", secret=secret,
                                   headers=headers, params={"per_page": limit})
            return (data or {}).get("conversations", [])

        if operation == "reply_conversation":
            conversation_id = config.get("conversation_id", "").strip()
            message = config.get("message", "").strip()
            admin_id = config.get("admin_id", "").strip()
            if not conversation_id or not message:
                raise ConnectorError("Conversation ID and reply body are required.")
            if not admin_id:
                raise ConnectorError("An admin ID is required to reply as a teammate.")
            return await self.call(
                "POST", f"/conversations/{conversation_id}/reply", secret=secret,
                headers=headers,
                json_body={"message_type": "comment", "type": "admin",
                           "admin_id": admin_id, "body": message},
            )

        raise NotImplementedError(operation)
