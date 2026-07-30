"""Marketing connectors: SendGrid, Mailchimp."""
from __future__ import annotations

import hashlib
from typing import Any

from ..base import FieldConfig, FieldType
from ..rest_base import ConnectorError, RestConnectorNode



class SendGridNode(RestConnectorNode):
    """Transactional email via SendGrid."""

    node_type = "sendgrid"
    name = "SendGrid"
    description = "Send transactional email and manage contacts via SendGrid"
    icon = "✉️"
    color = "#1a82e2"

    credential_slug = "sendgrid"
    auth_style = "bearer"
    base_url = "https://api.sendgrid.com/v3"

    fields = [
        FieldConfig(name="credential", label="SendGrid Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="sendgrid"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["send_email", "send_template", "add_contact"],
                    default="send_email"),
        FieldConfig(name="to", label="To", field_type=FieldType.STRING, required=False,
                    description="Comma-separated addresses"),
        FieldConfig(name="from_email", label="From", field_type=FieldType.STRING, required=False),
        FieldConfig(name="from_name", label="From Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="subject", label="Subject", field_type=FieldType.STRING, required=False),
        FieldConfig(name="body", label="Body", field_type=FieldType.STRING, required=False),
        FieldConfig(name="is_html", label="Send as HTML", field_type=FieldType.BOOLEAN,
                    required=False, default=False),
        FieldConfig(name="template_id", label="Template ID", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="template_data", label="Template Data", field_type=FieldType.JSON,
                    required=False),
    ]
    static_output_fields = ["delivered", "to", "status_code"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}

        if operation == "add_contact":
            email = config.get("to", "").strip()
            if not email:
                raise ConnectorError("An email address is required.")
            return await self.call(
                "PUT", "/marketing/contacts", secret=secret,
                json_body={"contacts": [{"email": email}]},
            )

        recipients = [e.strip() for e in (config.get("to") or "").split(",") if e.strip()]
        if not recipients:
            raise ConnectorError("At least one recipient is required.")

        from_email = (config.get("from_email") or creds.get("fromEmail")
                      or creds.get("from_email") or "").strip()
        if not from_email:
            raise ConnectorError("A verified 'From' address is required.")

        payload: dict[str, Any] = {
            "personalizations": [{"to": [{"email": e} for e in recipients]}],
            "from": {"email": from_email},
        }
        if config.get("from_name"):
            payload["from"]["name"] = config["from_name"]

        if operation == "send_template":
            template_id = config.get("template_id", "").strip()
            if not template_id:
                raise ConnectorError("Template ID is required.")
            payload["template_id"] = template_id
            template_data = config.get("template_data")
            if isinstance(template_data, str) and template_data.strip():
                import json
                try:
                    template_data = json.loads(template_data)
                except ValueError:
                    raise ConnectorError("Template data must be a JSON object.")
            if template_data:
                payload["personalizations"][0]["dynamic_template_data"] = template_data
        else:
            subject = config.get("subject", "").strip()
            body = config.get("body", "")
            if not subject or not body:
                raise ConnectorError("Subject and body are required.")
            payload["subject"] = subject
            payload["content"] = [{
                "type": "text/html" if config.get("is_html") else "text/plain",
                "value": body,
            }]

        # A successful send is 202 with an empty body.
        await self.call("POST", "/mail/send", secret=secret, json_body=payload)
        return {"delivered": True, "to": recipients, "operation": operation}


class MailchimpNode(RestConnectorNode):
    """Audiences and campaigns in Mailchimp."""

    node_type = "mailchimp"
    name = "Mailchimp"
    description = "Manage Mailchimp audience members and campaigns"
    icon = "🐵"
    color = "#ffe01b"

    credential_slug = "mailchimp"
    auth_style = "bearer"

    fields = [
        FieldConfig(name="credential", label="Mailchimp Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="mailchimp"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["add_member", "update_member", "get_member",
                             "list_members", "unsubscribe_member"],
                    default="add_member"),
        FieldConfig(name="list_id", label="Audience ID", field_type=FieldType.STRING),
        FieldConfig(name="email", label="Email", field_type=FieldType.STRING, required=False),
        FieldConfig(name="first_name", label="First Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="last_name", label="Last Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="status", label="Status", field_type=FieldType.SELECT,
                    options=["subscribed", "pending", "unsubscribed", "cleaned"],
                    required=False, default="subscribed"),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=50),
    ]
    static_output_fields = ["id", "email_address", "status"]

    @staticmethod
    def _subscriber_hash(email: str) -> str:
        """
        Mailchimp addresses a member by the MD5 of their lowercased email.

        MD5 here is Mailchimp's addressing scheme, not a security choice — it is
        how their URLs are formed, so there is nothing to harden.
        """
        return hashlib.md5(email.strip().lower().encode()).hexdigest()  # noqa: S324

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        # The datacentre suffix is part of the API key ("...-us21") and also the
        # hostname. Deriving it saves asking the user for something they already
        # supplied, and gets it right more often than they do.
        dc = (creds.get("dc") or creds.get("serverPrefix") or "").strip()
        if not dc and "-" in secret:
            dc = secret.rsplit("-", 1)[-1]
        if not dc:
            raise ConnectorError(
                "Could not determine the Mailchimp datacentre. Add it to the credential "
                "(e.g. 'us21')."
            )
        base = f"https://{dc}.api.mailchimp.com/3.0"

        list_id = config.get("list_id", "").strip()
        if not list_id:
            raise ConnectorError("Audience (list) ID is required.")

        if operation == "list_members":
            data = await self.call(
                "GET", f"{base}/lists/{list_id}/members", secret=secret,
                params={"count": min(int(config.get("limit") or 50), 1000)},
            )
            return (data or {}).get("members", [])

        email = config.get("email", "").strip()
        if not email:
            raise ConnectorError("An email address is required.")
        member_hash = self._subscriber_hash(email)

        if operation == "get_member":
            return await self.call("GET", f"{base}/lists/{list_id}/members/{member_hash}",
                                   secret=secret)

        if operation == "unsubscribe_member":
            return await self.call("PATCH", f"{base}/lists/{list_id}/members/{member_hash}",
                                   secret=secret, json_body={"status": "unsubscribed"})

        merge_fields = {}
        if config.get("first_name"):
            merge_fields["FNAME"] = config["first_name"]
        if config.get("last_name"):
            merge_fields["LNAME"] = config["last_name"]

        if operation == "add_member":
            body: dict[str, Any] = {
                "email_address": email,
                "status": config.get("status") or "subscribed",
            }
            if merge_fields:
                body["merge_fields"] = merge_fields
            # PUT upserts. POST fails with 400 "already a list member", which is
            # a confusing error for a workflow that simply ran twice.
            body["status_if_new"] = body["status"]
            return await self.call("PUT", f"{base}/lists/{list_id}/members/{member_hash}",
                                   secret=secret, json_body=body)

        if operation == "update_member":
            body = {}
            if config.get("status"):
                body["status"] = config["status"]
            if merge_fields:
                body["merge_fields"] = merge_fields
            if not body:
                raise ConnectorError("Nothing to update.")
            return await self.call("PATCH", f"{base}/lists/{list_id}/members/{member_hash}",
                                   secret=secret, json_body=body)

        raise NotImplementedError(operation)
