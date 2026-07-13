"""
Add Slack as a curated system MCP server.

Uses @modelcontextprotocol/server-slack (official Anthropic package).
Wired to the existing `slack` CredentialType (slug=slack, field=token).
"""
from django.db import migrations


def _add(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    if MCPServer.objects.filter(name="Slack", user__isnull=True).exists():
        return
    MCPServer.objects.create(
        name="Slack",
        type="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-slack"],
        env={},
        required_credential_types=["slack"],
        credential_env_map={"SLACK_BOT_TOKEN": "slack:token"},
        credential_header_map={},
        setup_notes=(
            "Read and post messages in Slack channels.\n"
            "Requires a `slack` credential with field: token.\n"
            "Create a Slack app at api.slack.com/apps, add Bot Token Scopes "
            "(channels:read, chat:write, etc.), install to your workspace, "
            "and paste the Bot User OAuth Token."
        ),
        enabled=True,
        user=None,
    )


def _remove(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPServer.objects.filter(name="Slack", user__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0006_fix_google_oauth_slug'),
    ]

    operations = [
        migrations.RunPython(_add, reverse_code=_remove),
    ]
