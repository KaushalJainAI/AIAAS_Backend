"""
Fix: seeded Google MCP servers used 'google_oauth' as the credential slug but
the actual CredentialType slug in the DB is 'google-oauth2'.
Update required_credential_types and credential_env_map on all affected rows.
"""
from django.db import migrations

_GOOGLE_SERVER_NAMES = [
    "Google Drive",
    "Gmail",
    "Google Calendar",
    "Google Sheets",
    "Google Docs",
]

_ENV_MAP = {
    "GDRIVE_CLIENT_ID": "google-oauth2:client_id",
    "GDRIVE_CLIENT_SECRET": "google-oauth2:client_secret",
    "GDRIVE_REFRESH_TOKEN": "google-oauth2:refresh_token",
}

_WORKSPACE_ENV_MAP = {
    "GOOGLE_CLIENT_ID": "google-oauth2:client_id",
    "GOOGLE_CLIENT_SECRET": "google-oauth2:client_secret",
    "GOOGLE_REFRESH_TOKEN": "google-oauth2:refresh_token",
}


def _fix(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    for server in MCPServer.objects.filter(name__in=_GOOGLE_SERVER_NAMES, user__isnull=True):
        server.required_credential_types = ["google-oauth2"]
        if server.name == "Google Drive":
            server.credential_env_map = _ENV_MAP
        else:
            server.credential_env_map = _WORKSPACE_ENV_MAP
        server.save(update_fields=["required_credential_types", "credential_env_map"])


def _unfix(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    for server in MCPServer.objects.filter(name__in=_GOOGLE_SERVER_NAMES, user__isnull=True):
        server.required_credential_types = ["google_oauth"]
        if server.name == "Google Drive":
            server.credential_env_map = {
                "GDRIVE_CLIENT_ID": "google_oauth:client_id",
                "GDRIVE_CLIENT_SECRET": "google_oauth:client_secret",
                "GDRIVE_REFRESH_TOKEN": "google_oauth:refresh_token",
            }
        else:
            server.credential_env_map = {
                "GOOGLE_CLIENT_ID": "google_oauth:client_id",
                "GOOGLE_CLIENT_SECRET": "google_oauth:client_secret",
                "GOOGLE_REFRESH_TOKEN": "google_oauth:refresh_token",
            }
        server.save(update_fields=["required_credential_types", "credential_env_map"])


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0005_seed_curated_mcp_servers'),
    ]

    operations = [
        migrations.RunPython(_fix, reverse_code=_unfix),
    ]
