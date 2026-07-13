"""
Data migration: replace all existing MCP servers with a curated set.

Credential-free connectors (testable immediately, no setup):
  - Filesystem, Fetch, Memory, Sequential Thinking

Google Workspace (require a `google_oauth` credential):
  - Google Drive, Gmail, Google Calendar, Google Sheets, Google Docs

Notion (requires a `notion` credential):
  - Notion
"""
from django.db import migrations


_CURATED_SERVERS = [
    # ------------------------------------------------------------------ #
    # Credential-free — testable without any API keys                     #
    # ------------------------------------------------------------------ #
    {
        "name": "Filesystem",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Read and write files under /tmp on the server. "
            "No credentials required. Change the path argument to restrict access."
        ),
        "enabled": True,
    },
    {
        "name": "Fetch",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-fetch"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Fetch HTML, JSON, or plain text from any public URL. "
            "No credentials required."
        ),
        "enabled": True,
    },
    {
        "name": "Memory",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Persistent key-value memory store shared across agent sessions. "
            "No credentials required."
        ),
        "enabled": True,
    },
    {
        "name": "Sequential Thinking",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Structured step-by-step reasoning tool for complex problem decomposition. "
            "No credentials required."
        ),
        "enabled": True,
    },
    # ------------------------------------------------------------------ #
    # Google Workspace — all share the `google_oauth` credential type.    #
    # Credential fields: client_id, client_secret, refresh_token          #
    # ------------------------------------------------------------------ #
    {
        "name": "Google Drive",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-gdrive"],
        "env": {},
        "required_credential_types": ["google_oauth"],
        "credential_env_map": {
            "GDRIVE_CLIENT_ID": "google_oauth:client_id",
            "GDRIVE_CLIENT_SECRET": "google_oauth:client_secret",
            "GDRIVE_REFRESH_TOKEN": "google_oauth:refresh_token",
        },
        "credential_header_map": {},
        "setup_notes": (
            "Search, read, and download files from Google Drive.\n"
            "Requires a `google_oauth` credential with fields: "
            "client_id, client_secret, refresh_token.\n"
            "Create an OAuth 2.0 client in Google Cloud Console "
            "(APIs & Services → Credentials) and grant Drive read/write scope."
        ),
        "enabled": True,
    },
    {
        "name": "Gmail",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@gptscript-ai/google-workspace-mcp"],
        "env": {"WORKSPACE_SCOPE": "gmail"},
        "required_credential_types": ["google_oauth"],
        "credential_env_map": {
            "GOOGLE_CLIENT_ID": "google_oauth:client_id",
            "GOOGLE_CLIENT_SECRET": "google_oauth:client_secret",
            "GOOGLE_REFRESH_TOKEN": "google_oauth:refresh_token",
        },
        "credential_header_map": {},
        "setup_notes": (
            "Read, search, and send Gmail messages.\n"
            "Requires a `google_oauth` credential with fields: "
            "client_id, client_secret, refresh_token.\n"
            "Enable the Gmail API in Google Cloud Console and grant mail scope."
        ),
        "enabled": True,
    },
    {
        "name": "Google Calendar",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@gptscript-ai/google-workspace-mcp"],
        "env": {"WORKSPACE_SCOPE": "calendar"},
        "required_credential_types": ["google_oauth"],
        "credential_env_map": {
            "GOOGLE_CLIENT_ID": "google_oauth:client_id",
            "GOOGLE_CLIENT_SECRET": "google_oauth:client_secret",
            "GOOGLE_REFRESH_TOKEN": "google_oauth:refresh_token",
        },
        "credential_header_map": {},
        "setup_notes": (
            "List, create, and update Google Calendar events.\n"
            "Requires a `google_oauth` credential with fields: "
            "client_id, client_secret, refresh_token.\n"
            "Enable the Calendar API in Google Cloud Console and grant calendar scope."
        ),
        "enabled": True,
    },
    {
        "name": "Google Sheets",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@gptscript-ai/google-workspace-mcp"],
        "env": {"WORKSPACE_SCOPE": "sheets"},
        "required_credential_types": ["google_oauth"],
        "credential_env_map": {
            "GOOGLE_CLIENT_ID": "google_oauth:client_id",
            "GOOGLE_CLIENT_SECRET": "google_oauth:client_secret",
            "GOOGLE_REFRESH_TOKEN": "google_oauth:refresh_token",
        },
        "credential_header_map": {},
        "setup_notes": (
            "Read and write Google Sheets spreadsheets.\n"
            "Requires a `google_oauth` credential with fields: "
            "client_id, client_secret, refresh_token.\n"
            "Enable the Sheets API in Google Cloud Console and grant spreadsheets scope."
        ),
        "enabled": True,
    },
    {
        "name": "Google Docs",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@gptscript-ai/google-workspace-mcp"],
        "env": {"WORKSPACE_SCOPE": "docs"},
        "required_credential_types": ["google_oauth"],
        "credential_env_map": {
            "GOOGLE_CLIENT_ID": "google_oauth:client_id",
            "GOOGLE_CLIENT_SECRET": "google_oauth:client_secret",
            "GOOGLE_REFRESH_TOKEN": "google_oauth:refresh_token",
        },
        "credential_header_map": {},
        "setup_notes": (
            "Read and edit Google Docs documents.\n"
            "Requires a `google_oauth` credential with fields: "
            "client_id, client_secret, refresh_token.\n"
            "Enable the Docs API in Google Cloud Console and grant documents scope."
        ),
        "enabled": True,
    },
    # ------------------------------------------------------------------ #
    # Notion — requires a `notion` credential type.                       #
    # Credential fields: api_key                                          #
    # ------------------------------------------------------------------ #
    {
        "name": "Notion",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-notion"],
        "env": {},
        "required_credential_types": ["notion"],
        "credential_env_map": {
            "NOTION_API_KEY": "notion:api_key",
        },
        "credential_header_map": {},
        "setup_notes": (
            "Search, read, and update Notion pages and databases.\n"
            "Requires a `notion` credential with field: api_key.\n"
            "Create an internal integration at notion.so/my-integrations, "
            "then share the target pages/databases with that integration."
        ),
        "enabled": True,
    },
]


def _seed(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPServer.objects.all().delete()
    for data in _CURATED_SERVERS:
        MCPServer.objects.create(user=None, **data)


def _unseed(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPServer.objects.filter(user__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0004_fix_name_uniqueness'),
    ]

    operations = [
        migrations.RunPython(_seed, reverse_code=_unseed),
    ]
