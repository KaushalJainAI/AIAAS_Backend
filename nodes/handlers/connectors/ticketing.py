"""Ticketing and project-management connectors: Jira, Linear, Asana, ClickUp, Todoist."""
from __future__ import annotations

import base64
from typing import Any

from ..base import FieldConfig, FieldType
from ..rest_base import ConnectorError, RestConnectorNode



class JiraNode(RestConnectorNode):
    """Issues in Jira Cloud."""

    node_type = "jira"
    name = "Jira"
    description = "Create, update, search and comment on Jira issues"
    icon = "🔵"
    color = "#0052cc"

    credential_slug = "jira"
    auth_style = "basic"

    fields = [
        FieldConfig(name="credential", label="Jira Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="jira"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_issue", "update_issue", "get_issue",
                             "search_issues", "add_comment", "transition_issue"],
                    default="create_issue"),
        FieldConfig(name="project_key", label="Project Key", field_type=FieldType.STRING,
                    required=False, placeholder="ENG"),
        FieldConfig(name="issue_key", label="Issue Key", field_type=FieldType.STRING,
                    required=False, placeholder="ENG-123"),
        FieldConfig(name="summary", label="Summary", field_type=FieldType.STRING, required=False),
        FieldConfig(name="description", label="Description", field_type=FieldType.STRING, required=False),
        FieldConfig(name="issue_type", label="Issue Type", field_type=FieldType.STRING,
                    required=False, default="Task"),
        FieldConfig(name="assignee_id", label="Assignee Account ID", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="jql", label="JQL", field_type=FieldType.STRING, required=False,
                    placeholder="project = ENG AND status = 'To Do'"),
        FieldConfig(name="comment", label="Comment", field_type=FieldType.STRING, required=False),
        FieldConfig(name="transition_id", label="Transition ID", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="limit", label="Max Results", field_type=FieldType.NUMBER,
                    required=False, default=50),
    ]
    static_output_fields = ["id", "key", "self"]

    @staticmethod
    def _adf(text: str) -> dict:
        """
        Wrap plain text in Atlassian Document Format.

        Jira Cloud rejects a plain string for description and comment bodies, so
        the obvious `{"description": "..."}` fails with a validation error that
        does not mention ADF at all.
        """
        return {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": text}]}],
        }

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        domain = (creds.get("domain") or creds.get("siteUrl") or "").strip().rstrip("/")
        email = (creds.get("email") or creds.get("username") or "").strip()
        token = (creds.get("apiToken") or creds.get("api_token") or creds.get("apiKey") or "").strip()
        if not domain or not email or not token:
            raise ConnectorError("Jira credential needs a domain, email and API token.")
        if not domain.startswith("http"):
            domain = f"https://{domain}"

        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        base = f"{domain}/rest/api/3"

        if operation == "create_issue":
            project = config.get("project_key", "").strip()
            summary = config.get("summary", "").strip()
            if not project or not summary:
                raise ConnectorError("Project key and summary are required.")
            fields: dict[str, Any] = {
                "project": {"key": project},
                "summary": summary,
                "issuetype": {"name": config.get("issue_type") or "Task"},
            }
            if config.get("description"):
                fields["description"] = self._adf(config["description"])
            if config.get("assignee_id"):
                fields["assignee"] = {"id": config["assignee_id"]}
            return await self.call("POST", f"{base}/issue", secret=basic,
                                   json_body={"fields": fields})

        issue_key = config.get("issue_key", "").strip()

        if operation == "get_issue":
            if not issue_key:
                raise ConnectorError("Issue key is required.")
            return await self.call("GET", f"{base}/issue/{issue_key}", secret=basic)

        if operation == "update_issue":
            if not issue_key:
                raise ConnectorError("Issue key is required.")
            fields = {}
            if config.get("summary"):
                fields["summary"] = config["summary"]
            if config.get("description"):
                fields["description"] = self._adf(config["description"])
            if config.get("assignee_id"):
                fields["assignee"] = {"id": config["assignee_id"]}
            if not fields:
                raise ConnectorError("Nothing to update — supply a summary, description or assignee.")
            await self.call("PUT", f"{base}/issue/{issue_key}", secret=basic,
                            json_body={"fields": fields})
            return {"key": issue_key, "updated": list(fields.keys())}

        if operation == "add_comment":
            comment = config.get("comment", "").strip()
            if not issue_key or not comment:
                raise ConnectorError("Issue key and comment are required.")
            return await self.call("POST", f"{base}/issue/{issue_key}/comment", secret=basic,
                                   json_body={"body": self._adf(comment)})

        if operation == "transition_issue":
            transition_id = config.get("transition_id", "").strip()
            if not issue_key or not transition_id:
                raise ConnectorError("Issue key and transition ID are required.")
            await self.call("POST", f"{base}/issue/{issue_key}/transitions", secret=basic,
                            json_body={"transition": {"id": transition_id}})
            return {"key": issue_key, "transitioned_to": transition_id}

        if operation == "search_issues":
            jql = config.get("jql", "").strip()
            if not jql:
                raise ConnectorError("A JQL query is required.")
            data = await self.call(
                "POST", f"{base}/search", secret=basic,
                json_body={"jql": jql, "maxResults": min(int(config.get("limit") or 50), 100)},
            )
            return (data or {}).get("issues", [])

        raise NotImplementedError(operation)


class LinearNode(RestConnectorNode):
    """Issues in Linear (GraphQL)."""

    node_type = "linear"
    name = "Linear"
    description = "Create, update and search Linear issues"
    icon = "▲"
    color = "#5e6ad2"

    credential_slug = "linear"
    auth_style = "header"
    auth_header = "Authorization"  # Linear takes the raw key, not "Bearer <key>"
    base_url = "https://api.linear.app/graphql"

    fields = [
        FieldConfig(name="credential", label="Linear Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="linear"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_issue", "update_issue", "get_issue", "list_issues"],
                    default="create_issue"),
        FieldConfig(name="team_id", label="Team ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="issue_id", label="Issue ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="title", label="Title", field_type=FieldType.STRING, required=False),
        FieldConfig(name="description", label="Description", field_type=FieldType.STRING, required=False),
        FieldConfig(name="priority", label="Priority (0-4)", field_type=FieldType.NUMBER,
                    required=False),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=25),
    ]
    static_output_fields = ["id", "identifier", "title", "url"]

    async def _graphql(self, query: str, variables: dict, secret: str) -> dict:
        body = await self.call("POST", self.base_url, secret=secret,
                               json_body={"query": query, "variables": variables})
        # GraphQL answers 200 with an errors array, so a failure would otherwise
        # sail through as success and produce a confusing empty result.
        if isinstance(body, dict) and body.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in body["errors"])
            raise ConnectorError(messages[:400])
        return (body or {}).get("data", {})

    async def run_operation(self, operation, config, secret, context):
        if operation == "create_issue":
            team_id = config.get("team_id", "").strip()
            title = config.get("title", "").strip()
            if not team_id or not title:
                raise ConnectorError("Team ID and title are required.")
            variables: dict[str, Any] = {"teamId": team_id, "title": title}
            if config.get("description"):
                variables["description"] = config["description"]
            if config.get("priority") not in (None, ""):
                variables["priority"] = int(config["priority"])
            query = """
                mutation CreateIssue($teamId: String!, $title: String!, $description: String, $priority: Int) {
                  issueCreate(input: {teamId: $teamId, title: $title, description: $description, priority: $priority}) {
                    success
                    issue { id identifier title url state { name } }
                  }
                }
            """
            data = await self._graphql(query, variables, secret)
            return data.get("issueCreate", {}).get("issue") or {}

        if operation == "update_issue":
            issue_id = config.get("issue_id", "").strip()
            if not issue_id:
                raise ConnectorError("Issue ID is required.")
            variables = {"id": issue_id}
            if config.get("title"):
                variables["title"] = config["title"]
            if config.get("description"):
                variables["description"] = config["description"]
            if len(variables) == 1:
                raise ConnectorError("Nothing to update — supply a title or description.")
            query = """
                mutation UpdateIssue($id: String!, $title: String, $description: String) {
                  issueUpdate(id: $id, input: {title: $title, description: $description}) {
                    success
                    issue { id identifier title url }
                  }
                }
            """
            data = await self._graphql(query, variables, secret)
            return data.get("issueUpdate", {}).get("issue") or {}

        if operation == "get_issue":
            issue_id = config.get("issue_id", "").strip()
            if not issue_id:
                raise ConnectorError("Issue ID is required.")
            query = """
                query GetIssue($id: String!) {
                  issue(id: $id) {
                    id identifier title description url
                    state { name } assignee { name email }
                  }
                }
            """
            data = await self._graphql(query, {"id": issue_id}, secret)
            return data.get("issue") or {}

        if operation == "list_issues":
            limit = min(int(config.get("limit") or 25), 100)
            query = """
                query ListIssues($first: Int!) {
                  issues(first: $first) {
                    nodes { id identifier title url state { name } }
                  }
                }
            """
            data = await self._graphql(query, {"first": limit}, secret)
            return data.get("issues", {}).get("nodes", [])

        raise NotImplementedError(operation)


class AsanaNode(RestConnectorNode):
    """Tasks in Asana."""

    node_type = "asana"
    name = "Asana"
    description = "Create, update and list Asana tasks"
    icon = "🔴"
    color = "#f06a6a"

    credential_slug = "asana"
    auth_style = "bearer"
    base_url = "https://app.asana.com/api/1.0"

    fields = [
        FieldConfig(name="credential", label="Asana Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="asana"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_task", "update_task", "get_task", "list_tasks", "add_comment"],
                    default="create_task"),
        FieldConfig(name="workspace_id", label="Workspace ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="project_id", label="Project ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="task_id", label="Task ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="task_name", label="Task Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="notes", label="Notes", field_type=FieldType.STRING, required=False),
        FieldConfig(name="completed", label="Mark Completed", field_type=FieldType.BOOLEAN,
                    required=False, default=False),
        FieldConfig(name="due_on", label="Due Date", field_type=FieldType.STRING,
                    required=False, placeholder="2026-08-15"),
        FieldConfig(name="comment", label="Comment", field_type=FieldType.STRING, required=False),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=50),
    ]
    static_output_fields = ["gid", "name", "completed", "permalink_url"]

    async def run_operation(self, operation, config, secret, context):
        if operation == "create_task":
            name = config.get("task_name", "").strip()
            if not name:
                raise ConnectorError("Task name is required.")
            payload: dict[str, Any] = {"name": name}
            if config.get("notes"):
                payload["notes"] = config["notes"]
            if config.get("due_on"):
                payload["due_on"] = config["due_on"]
            if config.get("project_id"):
                payload["projects"] = [config["project_id"]]
            elif config.get("workspace_id"):
                payload["workspace"] = config["workspace_id"]
            else:
                raise ConnectorError("Either a project ID or a workspace ID is required.")
            data = await self.call("POST", "/tasks", secret=secret, json_body={"data": payload})

        elif operation == "update_task":
            task_id = config.get("task_id", "").strip()
            if not task_id:
                raise ConnectorError("Task ID is required.")
            payload = {}
            if config.get("task_name"):
                payload["name"] = config["task_name"]
            if config.get("notes"):
                payload["notes"] = config["notes"]
            if config.get("due_on"):
                payload["due_on"] = config["due_on"]
            if config.get("completed") is not None:
                payload["completed"] = bool(config.get("completed"))
            data = await self.call("PUT", f"/tasks/{task_id}", secret=secret,
                                   json_body={"data": payload})

        elif operation == "get_task":
            task_id = config.get("task_id", "").strip()
            if not task_id:
                raise ConnectorError("Task ID is required.")
            data = await self.call("GET", f"/tasks/{task_id}", secret=secret)

        elif operation == "list_tasks":
            project_id = config.get("project_id", "").strip()
            if not project_id:
                raise ConnectorError("Project ID is required to list tasks.")
            data = await self.call("GET", f"/projects/{project_id}/tasks", secret=secret,
                                   params={"limit": min(int(config.get("limit") or 50), 100)})

        elif operation == "add_comment":
            task_id = config.get("task_id", "").strip()
            comment = config.get("comment", "").strip()
            if not task_id or not comment:
                raise ConnectorError("Task ID and comment are required.")
            data = await self.call("POST", f"/tasks/{task_id}/stories", secret=secret,
                                   json_body={"data": {"text": comment}})
        else:
            raise NotImplementedError(operation)

        # Asana wraps every response in {"data": ...}.
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data


class ClickUpNode(RestConnectorNode):
    """Tasks in ClickUp."""

    node_type = "clickup"
    name = "ClickUp"
    description = "Create, update and list ClickUp tasks"
    icon = "🟣"
    color = "#7b68ee"

    credential_slug = "clickup"
    auth_style = "header"
    auth_header = "Authorization"  # ClickUp takes the raw token
    base_url = "https://api.clickup.com/api/v2"

    fields = [
        FieldConfig(name="credential", label="ClickUp Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="clickup"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_task", "update_task", "get_task", "list_tasks"],
                    default="create_task"),
        FieldConfig(name="list_id", label="List ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="task_id", label="Task ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="task_name", label="Task Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="description", label="Description", field_type=FieldType.STRING, required=False),
        FieldConfig(name="status", label="Status", field_type=FieldType.STRING, required=False),
        FieldConfig(name="priority", label="Priority (1-4)", field_type=FieldType.NUMBER, required=False),
    ]
    static_output_fields = ["id", "name", "url", "status"]

    async def run_operation(self, operation, config, secret, context):
        if operation == "create_task":
            list_id = config.get("list_id", "").strip()
            name = config.get("task_name", "").strip()
            if not list_id or not name:
                raise ConnectorError("List ID and task name are required.")
            payload: dict[str, Any] = {"name": name}
            if config.get("description"):
                payload["description"] = config["description"]
            if config.get("status"):
                payload["status"] = config["status"]
            if config.get("priority") not in (None, ""):
                payload["priority"] = int(config["priority"])
            return await self.call("POST", f"/list/{list_id}/task", secret=secret,
                                   json_body=payload)

        if operation == "update_task":
            task_id = config.get("task_id", "").strip()
            if not task_id:
                raise ConnectorError("Task ID is required.")
            payload = {}
            for src, dst in (("task_name", "name"), ("description", "description"),
                             ("status", "status")):
                if config.get(src):
                    payload[dst] = config[src]
            if config.get("priority") not in (None, ""):
                payload["priority"] = int(config["priority"])
            if not payload:
                raise ConnectorError("Nothing to update.")
            return await self.call("PUT", f"/task/{task_id}", secret=secret, json_body=payload)

        if operation == "get_task":
            task_id = config.get("task_id", "").strip()
            if not task_id:
                raise ConnectorError("Task ID is required.")
            return await self.call("GET", f"/task/{task_id}", secret=secret)

        if operation == "list_tasks":
            list_id = config.get("list_id", "").strip()
            if not list_id:
                raise ConnectorError("List ID is required.")
            data = await self.call("GET", f"/list/{list_id}/task", secret=secret)
            return (data or {}).get("tasks", [])

        raise NotImplementedError(operation)


class TodoistNode(RestConnectorNode):
    """Tasks in Todoist."""

    node_type = "todoist"
    name = "Todoist"
    description = "Create, complete and list Todoist tasks"
    icon = "✅"
    color = "#e44332"

    credential_slug = "todoist"
    auth_style = "bearer"
    base_url = "https://api.todoist.com/rest/v2"

    fields = [
        FieldConfig(name="credential", label="Todoist Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="todoist"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_task", "complete_task", "list_tasks", "get_task"],
                    default="create_task"),
        FieldConfig(name="content", label="Task", field_type=FieldType.STRING, required=False),
        FieldConfig(name="description", label="Description", field_type=FieldType.STRING, required=False),
        FieldConfig(name="task_id", label="Task ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="project_id", label="Project ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="due_string", label="Due", field_type=FieldType.STRING,
                    required=False, placeholder="tomorrow at 10am"),
        FieldConfig(name="priority", label="Priority (1-4)", field_type=FieldType.NUMBER, required=False),
    ]
    static_output_fields = ["id", "content", "url", "is_completed"]

    async def run_operation(self, operation, config, secret, context):
        if operation == "create_task":
            content = config.get("content", "").strip()
            if not content:
                raise ConnectorError("Task content is required.")
            payload: dict[str, Any] = {"content": content}
            for src, dst in (("description", "description"), ("project_id", "project_id"),
                             ("due_string", "due_string")):
                if config.get(src):
                    payload[dst] = config[src]
            if config.get("priority") not in (None, ""):
                payload["priority"] = int(config["priority"])
            return await self.call("POST", "/tasks", secret=secret, json_body=payload)

        if operation == "complete_task":
            task_id = config.get("task_id", "").strip()
            if not task_id:
                raise ConnectorError("Task ID is required.")
            # Answers 204 with no body.
            await self.call("POST", f"/tasks/{task_id}/close", secret=secret)
            return {"id": task_id, "is_completed": True}

        if operation == "get_task":
            task_id = config.get("task_id", "").strip()
            if not task_id:
                raise ConnectorError("Task ID is required.")
            return await self.call("GET", f"/tasks/{task_id}", secret=secret)

        if operation == "list_tasks":
            params = {}
            if config.get("project_id"):
                params["project_id"] = config["project_id"]
            return await self.call("GET", "/tasks", secret=secret, params=params or None)

        raise NotImplementedError(operation)
