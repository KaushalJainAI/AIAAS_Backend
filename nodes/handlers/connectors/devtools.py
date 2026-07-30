"""Developer-tooling connectors: GitLab, Sentry, PagerDuty."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..base import FieldConfig, FieldType
from ..rest_base import ConnectorError, RestConnectorNode



class GitLabNode(RestConnectorNode):
    """Issues and merge requests in GitLab."""

    node_type = "gitlab"
    name = "GitLab"
    description = "Manage GitLab issues, merge requests and pipelines"
    icon = "🦊"
    color = "#fc6d26"

    credential_slug = "gitlab"
    auth_style = "header"
    auth_header = "PRIVATE-TOKEN"

    fields = [
        FieldConfig(name="credential", label="GitLab Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="gitlab"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_issue", "list_issues", "get_issue", "comment_issue",
                             "create_merge_request", "trigger_pipeline"],
                    default="create_issue"),
        FieldConfig(name="project", label="Project", field_type=FieldType.STRING,
                    required=False, placeholder="group/project or numeric ID"),
        FieldConfig(name="title", label="Title", field_type=FieldType.STRING, required=False),
        FieldConfig(name="description", label="Description", field_type=FieldType.STRING, required=False),
        FieldConfig(name="issue_iid", label="Issue IID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="comment", label="Comment", field_type=FieldType.STRING, required=False),
        FieldConfig(name="source_branch", label="Source Branch", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="target_branch", label="Target Branch", field_type=FieldType.STRING,
                    required=False, default="main"),
        FieldConfig(name="ref", label="Pipeline Ref", field_type=FieldType.STRING,
                    required=False, default="main"),
        FieldConfig(name="labels", label="Labels", field_type=FieldType.STRING,
                    required=False, description="Comma separated"),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=50),
    ]
    static_output_fields = ["id", "iid", "web_url", "title", "state"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        host = (creds.get("baseUrl") or creds.get("host") or "https://gitlab.com").strip().rstrip("/")
        base = f"{host}/api/v4"

        project = config.get("project", "").strip()
        if not project:
            raise ConnectorError("Project is required.")
        # A path like "group/project" has to be URL-encoded whole, slash included,
        # or GitLab reads it as extra path segments and answers 404.
        project_ref = quote(project, safe="")

        if operation == "create_issue":
            title = config.get("title", "").strip()
            if not title:
                raise ConnectorError("Title is required.")
            payload: dict[str, Any] = {"title": title}
            if config.get("description"):
                payload["description"] = config["description"]
            if config.get("labels"):
                payload["labels"] = config["labels"]
            return await self.call("POST", f"{base}/projects/{project_ref}/issues",
                                   secret=secret, json_body=payload)

        if operation == "list_issues":
            return await self.call(
                "GET", f"{base}/projects/{project_ref}/issues", secret=secret,
                params={"per_page": min(int(config.get("limit") or 50), 100)},
            )

        if operation == "get_issue":
            iid = config.get("issue_iid", "").strip()
            if not iid:
                raise ConnectorError("Issue IID is required.")
            return await self.call("GET", f"{base}/projects/{project_ref}/issues/{iid}",
                                   secret=secret)

        if operation == "comment_issue":
            iid = config.get("issue_iid", "").strip()
            comment = config.get("comment", "").strip()
            if not iid or not comment:
                raise ConnectorError("Issue IID and comment are required.")
            return await self.call("POST", f"{base}/projects/{project_ref}/issues/{iid}/notes",
                                   secret=secret, json_body={"body": comment})

        if operation == "create_merge_request":
            source = config.get("source_branch", "").strip()
            title = config.get("title", "").strip()
            if not source or not title:
                raise ConnectorError("Source branch and title are required.")
            payload = {
                "source_branch": source,
                "target_branch": config.get("target_branch") or "main",
                "title": title,
            }
            if config.get("description"):
                payload["description"] = config["description"]
            return await self.call("POST", f"{base}/projects/{project_ref}/merge_requests",
                                   secret=secret, json_body=payload)

        if operation == "trigger_pipeline":
            return await self.call("POST", f"{base}/projects/{project_ref}/pipeline",
                                   secret=secret,
                                   params={"ref": config.get("ref") or "main"})

        raise NotImplementedError(operation)


class SentryNode(RestConnectorNode):
    """Issues in Sentry."""

    node_type = "sentry"
    name = "Sentry"
    description = "List, inspect and resolve Sentry issues"
    icon = "🟥"
    color = "#362d59"

    credential_slug = "sentry"
    auth_style = "bearer"
    base_url = "https://sentry.io/api/0"

    fields = [
        FieldConfig(name="credential", label="Sentry Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="sentry"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["list_issues", "get_issue", "resolve_issue", "ignore_issue"],
                    default="list_issues"),
        FieldConfig(name="organization", label="Organisation Slug", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="project", label="Project Slug", field_type=FieldType.STRING, required=False),
        FieldConfig(name="issue_id", label="Issue ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="query", label="Search Query", field_type=FieldType.STRING,
                    required=False, default="is:unresolved"),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=25),
    ]
    static_output_fields = ["id", "title", "culprit", "status", "count", "permalink"]

    async def run_operation(self, operation, config, secret, context):
        if operation in ("resolve_issue", "ignore_issue"):
            issue_id = config.get("issue_id", "").strip()
            if not issue_id:
                raise ConnectorError("Issue ID is required.")
            status = "resolved" if operation == "resolve_issue" else "ignored"
            return await self.call("PUT", f"/issues/{issue_id}/", secret=secret,
                                   json_body={"status": status})

        if operation == "get_issue":
            issue_id = config.get("issue_id", "").strip()
            if not issue_id:
                raise ConnectorError("Issue ID is required.")
            return await self.call("GET", f"/issues/{issue_id}/", secret=secret)

        if operation == "list_issues":
            org = config.get("organization", "").strip()
            project = config.get("project", "").strip()
            if not org or not project:
                raise ConnectorError("Organisation and project slugs are required.")
            return await self.call(
                "GET", f"/projects/{org}/{project}/issues/", secret=secret,
                params={
                    "query": config.get("query") or "is:unresolved",
                    "limit": min(int(config.get("limit") or 25), 100),
                },
            )

        raise NotImplementedError(operation)


class PagerDutyNode(RestConnectorNode):
    """Incidents in PagerDuty."""

    node_type = "pagerduty"
    name = "PagerDuty"
    description = "Trigger, acknowledge and resolve PagerDuty incidents"
    icon = "🚨"
    color = "#06ac38"

    credential_slug = "pagerduty"
    auth_style = "header"
    auth_header = "Authorization"  # Format is "Token token=<key>"
    base_url = "https://api.pagerduty.com"

    fields = [
        FieldConfig(name="credential", label="PagerDuty Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="pagerduty"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_incident", "list_incidents", "get_incident",
                             "acknowledge_incident", "resolve_incident"],
                    default="create_incident"),
        FieldConfig(name="service_id", label="Service ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="title", label="Title", field_type=FieldType.STRING, required=False),
        FieldConfig(name="details", label="Details", field_type=FieldType.STRING, required=False),
        FieldConfig(name="urgency", label="Urgency", field_type=FieldType.SELECT,
                    options=["high", "low"], required=False, default="high"),
        FieldConfig(name="incident_id", label="Incident ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="from_email", label="From Email", field_type=FieldType.STRING,
                    required=False,
                    description="PagerDuty requires the email of an existing user for writes"),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=25),
    ]
    static_output_fields = ["id", "incident_number", "status", "title", "html_url"]

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        # PagerDuty wants "Token token=abc123", not a plain bearer.
        auth = f"Token token={secret}"
        headers = {"Accept": "application/vnd.pagerduty+json;version=2"}

        from_email = (config.get("from_email") or creds.get("fromEmail")
                      or creds.get("from_email") or "").strip()

        if operation == "list_incidents":
            return (await self.call(
                "GET", "/incidents", secret=auth, headers=headers,
                params={"limit": min(int(config.get("limit") or 25), 100)},
            ) or {}).get("incidents", [])

        if operation == "get_incident":
            incident_id = config.get("incident_id", "").strip()
            if not incident_id:
                raise ConnectorError("Incident ID is required.")
            return (await self.call("GET", f"/incidents/{incident_id}", secret=auth,
                                    headers=headers) or {}).get("incident", {})

        # Everything below writes, and PagerDuty rejects writes without a From
        # header naming a real user — a 400 whose message does not make the cause
        # obvious, so check it here.
        if not from_email:
            raise ConnectorError(
                "PagerDuty requires a 'From Email' of an existing user for this operation."
            )
        headers["From"] = from_email

        if operation == "create_incident":
            service_id = config.get("service_id", "").strip()
            title = config.get("title", "").strip()
            if not service_id or not title:
                raise ConnectorError("Service ID and title are required.")
            payload: dict[str, Any] = {
                "incident": {
                    "type": "incident",
                    "title": title,
                    "service": {"id": service_id, "type": "service_reference"},
                    "urgency": config.get("urgency") or "high",
                }
            }
            if config.get("details"):
                payload["incident"]["body"] = {
                    "type": "incident_body", "details": config["details"],
                }
            return (await self.call("POST", "/incidents", secret=auth, headers=headers,
                                    json_body=payload) or {}).get("incident", {})

        if operation in ("acknowledge_incident", "resolve_incident"):
            incident_id = config.get("incident_id", "").strip()
            if not incident_id:
                raise ConnectorError("Incident ID is required.")
            status = "acknowledged" if operation == "acknowledge_incident" else "resolved"
            return (await self.call(
                "PUT", f"/incidents/{incident_id}", secret=auth, headers=headers,
                json_body={"incident": {"type": "incident_reference", "status": status}},
            ) or {}).get("incident", {})

        raise NotImplementedError(operation)
