"""
Data migration: move connector presentation metadata into the database.

This copy of the metadata previously lived in the frontend as a hardcoded
`CONNECTOR_META` map in `pages/Connectors.tsx`, keyed on `MCPServer.name`. That
made the catalog a code dependency: seeding a server backend-side rendered it
with a generic icon and no description until someone edited React, and
user-created servers could never have presentation at all.

Keyed on `name` here only because that is the identifier these rows already
have; from now on the frontend keys off `icon_slug`.
"""
from django.db import migrations


# name -> (display_name, category, tagline, icon_slug, help_url)
_PRESENTATION = {
    'Google Drive': (
        'Google Drive', 'google_workspace',
        'Search, read, and manage files in your Drive',
        'google-drive', 'https://console.cloud.google.com/apis/credentials',
    ),
    'Gmail': (
        'Gmail', 'google_workspace',
        'Read, search, and send emails via Gmail',
        'gmail', 'https://console.cloud.google.com/apis/credentials',
    ),
    'Google Calendar': (
        'Google Calendar', 'google_workspace',
        'List, create, and update calendar events',
        'google-calendar', 'https://console.cloud.google.com/apis/credentials',
    ),
    'Google Sheets': (
        'Google Sheets', 'google_workspace',
        'Read and write spreadsheet data',
        'google-sheets', 'https://console.cloud.google.com/apis/credentials',
    ),
    'Google Docs': (
        'Google Docs', 'google_workspace',
        'Read and edit documents',
        'google-docs', 'https://console.cloud.google.com/apis/credentials',
    ),
    'Notion': (
        'Notion', 'productivity',
        'Search and update pages and databases',
        'notion', 'https://www.notion.so/my-integrations',
    ),
    'Slack': (
        'Slack', 'communication',
        'Read and post messages in Slack channels',
        'slack', 'https://api.slack.com/apps',
    ),
    'Filesystem': (
        'Files', 'utilities',
        'Read and write files in the workspace folder',
        'filesystem', '',
    ),
    'Fetch': (
        'Web pages', 'utilities',
        'Read the contents of any public web page',
        'fetch', '',
    ),
    'Memory': (
        'Long-term memory', 'utilities',
        'Remember facts across separate conversations',
        'memory', '',
    ),
    'Sequential Thinking': (
        'Step-by-step reasoning', 'utilities',
        'Work through complex problems one step at a time',
        'sequential-thinking', '',
    ),
}


def backfill(apps, schema_editor):
    MCPServer = apps.get_model('mcp_integration', 'MCPServer')
    for name, (display, category, tagline, icon, help_url) in _PRESENTATION.items():
        # System rows only (user IS NULL). A user's own server that happens to
        # share a curated name is theirs, and overwriting its copy would be a
        # surprise edit to something they configured by hand.
        MCPServer.objects.filter(name=name, user__isnull=True).update(
            display_name=display,
            category=category,
            tagline=tagline,
            icon_slug=icon,
            help_url=help_url,
        )


def unbackfill(apps, schema_editor):
    MCPServer = apps.get_model('mcp_integration', 'MCPServer')
    MCPServer.objects.filter(name__in=_PRESENTATION, user__isnull=True).update(
        display_name='', category='custom', tagline='', icon_slug='', help_url='',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0008_mcpserver_category_mcpserver_display_name_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
