"""CRM connectors: HubSpot, Pipedrive, Salesforce."""
from __future__ import annotations

from typing import Any

from ..base import FieldConfig, FieldType
from ..rest_base import ConnectorError, RestConnectorNode



def _parse_props(raw: Any) -> dict:
    """
    Accept extra properties as either a dict or a JSON string.

    The JSON field type hands back a parsed dict, but a value that arrived
    through an upstream expression is often still a string. Rejecting the string
    would make the node fail depending on how the value was wired, which is a
    confusing thing to debug.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        import json
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            raise ConnectorError("Additional properties must be a JSON object.")
    return {}


class HubSpotNode(RestConnectorNode):
    """Contacts, companies and deals in HubSpot."""

    node_type = "hubspot"
    name = "HubSpot"
    description = "Manage HubSpot contacts, companies and deals"
    icon = "🟠"
    color = "#ff7a59"

    credential_slug = "hubspot"
    auth_style = "bearer"
    base_url = "https://api.hubapi.com"

    fields = [
        FieldConfig(name="credential", label="HubSpot Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="hubspot"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_contact", "update_contact", "get_contact",
                             "search_contacts", "create_deal", "create_company"],
                    default="create_contact"),
        FieldConfig(name="email", label="Email", field_type=FieldType.STRING, required=False),
        FieldConfig(name="contact_id", label="Contact ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="firstname", label="First Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="lastname", label="Last Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="deal_name", label="Deal Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="amount", label="Deal Amount", field_type=FieldType.STRING, required=False),
        FieldConfig(name="company_name", label="Company Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="query", label="Search Query", field_type=FieldType.STRING, required=False),
        FieldConfig(name="properties", label="Additional Properties", field_type=FieldType.JSON,
                    required=False, description="Any other HubSpot properties, as JSON"),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=25),
    ]
    static_output_fields = ["id", "properties", "createdAt"]

    async def run_operation(self, operation, config, secret, context):
        extra = _parse_props(config.get("properties"))
        limit = min(int(config.get("limit") or 25), 100)

        if operation == "create_contact":
            email = config.get("email", "").strip()
            if not email:
                raise ConnectorError("Email is required to create a contact.")
            props = {"email": email, **extra}
            for key in ("firstname", "lastname"):
                if config.get(key):
                    props[key] = config[key]
            return await self.call("POST", "/crm/v3/objects/contacts", secret=secret,
                                   json_body={"properties": props})

        if operation == "update_contact":
            contact_id = config.get("contact_id", "").strip()
            if not contact_id:
                raise ConnectorError("Contact ID is required to update a contact.")
            props = dict(extra)
            for key in ("email", "firstname", "lastname"):
                if config.get(key):
                    props[key] = config[key]
            if not props:
                raise ConnectorError("Nothing to update — supply at least one property.")
            return await self.call("PATCH", f"/crm/v3/objects/contacts/{contact_id}",
                                   secret=secret, json_body={"properties": props})

        if operation == "get_contact":
            contact_id = config.get("contact_id", "").strip()
            if not contact_id:
                raise ConnectorError("Contact ID is required.")
            return await self.call("GET", f"/crm/v3/objects/contacts/{contact_id}", secret=secret)

        if operation == "search_contacts":
            query = config.get("query", "").strip()
            if not query:
                raise ConnectorError("Search query is required.")
            data = await self.call(
                "POST", "/crm/v3/objects/contacts/search", secret=secret,
                json_body={"query": query, "limit": limit},
            )
            return (data or {}).get("results", [])

        if operation == "create_deal":
            deal_name = config.get("deal_name", "").strip()
            if not deal_name:
                raise ConnectorError("Deal name is required.")
            props = {"dealname": deal_name, **extra}
            if config.get("amount"):
                props["amount"] = str(config["amount"])
            return await self.call("POST", "/crm/v3/objects/deals", secret=secret,
                                   json_body={"properties": props})

        if operation == "create_company":
            company = config.get("company_name", "").strip()
            if not company:
                raise ConnectorError("Company name is required.")
            return await self.call("POST", "/crm/v3/objects/companies", secret=secret,
                                   json_body={"properties": {"name": company, **extra}})

        raise NotImplementedError(operation)


class PipedriveNode(RestConnectorNode):
    """Persons, deals and notes in Pipedrive."""

    node_type = "pipedrive"
    name = "Pipedrive"
    description = "Manage Pipedrive persons, deals and notes"
    icon = "🟩"
    color = "#017737"

    credential_slug = "pipedrive"
    # Pipedrive authenticates with an api_token query parameter, not a header.
    auth_style = "none"

    fields = [
        FieldConfig(name="credential", label="Pipedrive Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="pipedrive"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_person", "create_deal", "get_deal",
                             "list_deals", "add_note"], default="create_person"),
        FieldConfig(name="name", label="Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="email", label="Email", field_type=FieldType.STRING, required=False),
        FieldConfig(name="phone", label="Phone", field_type=FieldType.STRING, required=False),
        FieldConfig(name="title", label="Deal Title", field_type=FieldType.STRING, required=False),
        FieldConfig(name="value", label="Deal Value", field_type=FieldType.STRING, required=False),
        FieldConfig(name="deal_id", label="Deal ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="content", label="Note Content", field_type=FieldType.STRING, required=False),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=50),
    ]
    static_output_fields = ["id", "name", "add_time"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        token = (creds.get("apiToken") or creds.get("api_token") or creds.get("apiKey") or "").strip()
        if not token:
            raise ConnectorError("Pipedrive credential has no API token.")
        domain = (creds.get("companyDomain") or creds.get("company_domain") or "").strip()
        base = f"https://{domain}.pipedrive.com/api/v1" if domain else "https://api.pipedrive.com/v1"

        params = {"api_token": token}
        limit = min(int(config.get("limit") or 50), 100)

        if operation == "create_person":
            name = config.get("name", "").strip()
            if not name:
                raise ConnectorError("Name is required to create a person.")
            body: dict[str, Any] = {"name": name}
            if config.get("email"):
                body["email"] = [config["email"]]
            if config.get("phone"):
                body["phone"] = [config["phone"]]
            data = await self.call("POST", f"{base}/persons", params=params, json_body=body)

        elif operation == "create_deal":
            title = config.get("title", "").strip()
            if not title:
                raise ConnectorError("Deal title is required.")
            body = {"title": title}
            if config.get("value"):
                body["value"] = config["value"]
            data = await self.call("POST", f"{base}/deals", params=params, json_body=body)

        elif operation == "get_deal":
            deal_id = config.get("deal_id", "").strip()
            if not deal_id:
                raise ConnectorError("Deal ID is required.")
            data = await self.call("GET", f"{base}/deals/{deal_id}", params=params)

        elif operation == "list_deals":
            data = await self.call("GET", f"{base}/deals",
                                   params={**params, "limit": limit})

        elif operation == "add_note":
            content = config.get("content", "").strip()
            deal_id = config.get("deal_id", "").strip()
            if not content or not deal_id:
                raise ConnectorError("Note content and deal ID are required.")
            data = await self.call("POST", f"{base}/notes", params=params,
                                   json_body={"content": content, "deal_id": int(deal_id)})
        else:
            raise NotImplementedError(operation)

        # Pipedrive wraps everything in {success, data}. Unwrap so downstream
        # nodes see the record rather than the envelope.
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data


class SalesforceNode(RestConnectorNode):
    """Records and SOQL queries in Salesforce."""

    node_type = "salesforce"
    name = "Salesforce"
    description = "Create, update and query Salesforce records"
    icon = "☁️"
    color = "#00a1e0"

    credential_slug = "salesforce"
    credential_key = "accessToken"
    auth_style = "bearer"

    fields = [
        FieldConfig(name="credential", label="Salesforce Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="salesforce"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_record", "update_record", "get_record", "query"],
                    default="create_record"),
        FieldConfig(name="sobject", label="Object Type", field_type=FieldType.STRING,
                    required=False, default="Lead",
                    description="Lead, Contact, Account, Opportunity, …"),
        FieldConfig(name="record_id", label="Record ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="record_data", label="Record Fields", field_type=FieldType.JSON,
                    required=False, description="Field values as JSON"),
        FieldConfig(name="soql", label="SOQL Query", field_type=FieldType.STRING, required=False,
                    placeholder="SELECT Id, Name FROM Lead LIMIT 10"),
        FieldConfig(name="api_version", label="API Version", field_type=FieldType.STRING,
                    required=False, default="v60.0"),
    ]
    static_output_fields = ["id", "success"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        instance = (creds.get("instanceUrl") or creds.get("instance_url") or "").strip().rstrip("/")
        if not instance:
            raise ConnectorError("Salesforce credential has no instance URL.")

        version = config.get("api_version") or "v60.0"
        base = f"{instance}/services/data/{version}"
        sobject = (config.get("sobject") or "Lead").strip()

        if operation == "query":
            soql = config.get("soql", "").strip()
            if not soql:
                raise ConnectorError("A SOQL query is required.")
            data = await self.call("GET", f"{base}/query", secret=secret, params={"q": soql})
            return (data or {}).get("records", [])

        if operation == "get_record":
            record_id = config.get("record_id", "").strip()
            if not record_id:
                raise ConnectorError("Record ID is required.")
            return await self.call("GET", f"{base}/sobjects/{sobject}/{record_id}", secret=secret)

        record_data = _parse_props(config.get("record_data"))
        if operation == "create_record":
            if not record_data:
                raise ConnectorError("Record fields are required to create a record.")
            return await self.call("POST", f"{base}/sobjects/{sobject}", secret=secret,
                                   json_body=record_data)

        if operation == "update_record":
            record_id = config.get("record_id", "").strip()
            if not record_id:
                raise ConnectorError("Record ID is required to update a record.")
            if not record_data:
                raise ConnectorError("Record fields are required to update a record.")
            # Salesforce PATCH answers 204 with no body; report the id so the
            # downstream node has something to key on.
            await self.call("PATCH", f"{base}/sobjects/{sobject}/{record_id}",
                            secret=secret, json_body=record_data)
            return {"id": record_id, "success": True, "updated": list(record_data.keys())}

        raise NotImplementedError(operation)
