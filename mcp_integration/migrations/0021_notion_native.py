"""
Serve Notion natively; point the Slack card at Messaging.

The Notion row is stdio (`@notionhq/notion-mcp-server`), and production runs
no Node (`MCP_ALLOW_STDIO=False`), so the card sat on "Coming soon" with a
credential that could never activate it. Notion's API needs only a bearer
token from an internal integration, so `chat/tools/notion.py` calls it from
this process — search, read page, query database, create page — and the row
becomes `type='native'`, exactly as Gmail/Drive/Sheets/Calendar did in 0019.
The Connections switch, the `notion` credential and agents' `connectors`
scopes are keyed on the row and keep working; only the process wiring is
cleared. Its stored tool catalogue is dropped: a catalogue row for a native
server would be a second, stale answer to a question the registry now owns.

The Slack MCP row stays `coming_soon` (its process still cannot start), but
its notes stop pretending messaging is future: reading and posting already
works through the Messaging section with a bot token, so the card says that
instead of offering a setup that ships nothing.
"""
from django.db import migrations

NOTION_NOTE = (
    "Search pages and databases, read a page, and create pages.\n"
    "Click Connect and paste the internal integration token from "
    "notion.so/my-integrations (share pages with the integration first, or "
    "Notion reports them as not found).\n"
)

SLACK_NOTE = (
    "Coming soon — this card's own server cannot start on this platform.\n\n"
    "Reading and posting already works through the Messaging section below "
    "with a Slack bot token: no setup here will activate this card."
)

# Verbatim pre-images for the reverse, as served on 2026-09-22.
NOTION_WAS = (
    "Coming soon — search and update Notion pages and databases.\n\n"
    "It will ask for a Notion token once it ships."
)
SLACK_WAS = (
    "Coming soon — read and post Slack messages, and list channels and users.\n\n"
    "It will walk through creating a Slack app once it ships."
)


def _to_native(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPToolCatalogue = apps.get_model("mcp_integration", "MCPToolCatalogue")

    MCPServer.objects.filter(name="Notion", user__isnull=True).update(
        type="native",
        command=None,
        args=[],
        url=None,
        env={},
        credential_env_map={},
        credential_header_map={},
        credential_file_map={},
        required_credential_types=["notion"],
        setup_notes=NOTION_NOTE,
        enabled=True,
        coming_soon=False,
    )
    MCPToolCatalogue.objects.filter(
        server__user__isnull=True, server__name="Notion",
    ).delete()

    MCPServer.objects.filter(name="Slack", user__isnull=True).update(
        setup_notes=SLACK_NOTE,
    )


def _from_native(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPServer.objects.filter(name="Notion", user__isnull=True).update(
        type="stdio",
        command="npx",
        args=["-y", "@notionhq/notion-mcp-server"],
        env={},
        credential_env_map={"NOTION_TOKEN": "notion:token"},
        required_credential_types=["notion"],
        setup_notes=NOTION_WAS,
        enabled=False,
        coming_soon=True,
    )
    MCPServer.objects.filter(name="Slack", user__isnull=True).update(
        setup_notes=SLACK_WAS,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("mcp_integration", "0020_trim_coming_soon_notes"),
    ]

    operations = [
        migrations.RunPython(_to_native, reverse_code=_from_native),
    ]
