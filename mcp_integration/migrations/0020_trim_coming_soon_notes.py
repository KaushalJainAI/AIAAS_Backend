"""Trim the coming-soon setup notes to what a user can act on: nothing.

Migration 0017 announced Notion and Slack by prepending "Coming soon" to their
full setup notes. That left each card reading as instructions — credential
field names in backticks, dashboard URLs, token prefixes — for a connector
whose Connect button does not exist. A user cannot act on any of it, so the
card is noise shaped like homework.

The fix is data, not rendering: `setup_notes` is served verbatim to every
client (web frontend, BrowserOS), so clamping it in one UI would leave the
jargon one screen away. The trimmed note keeps the what-it-does sentence and
one plain line about what will be asked for once it ships.

Reversing restores the 0017 text exactly, so the full notes survive for the
day these connectors actually ship.
"""

from django.db import migrations


# Curated rows only — a user's own server is theirs, and nothing here renames it.
SHORT_NOTES = {
    "Notion": (
        "Coming soon — search and update Notion pages and databases.\n"
        "\n"
        "It will ask for a Notion token once it ships."
    ),
    "Slack": (
        "Coming soon — read and post Slack messages, and list channels and users.\n"
        "\n"
        "It will walk through creating a Slack app once it ships."
    ),
}

# Exactly what 0017 left behind, so the reverse restores it byte for byte.
LONG_NOTES = {
    "Notion": (
        "Coming soon — this connector is not available yet. The setup below is "
        "what it will ask for once it ships.\n"
        "\n"
        "Search, read, and update Notion pages and databases.\n"
        "Requires a `notion` credential with field: token.\n"
        "Create an internal integration at notion.so/my-integrations, then share "
        "the target pages or databases with that integration — a token alone sees "
        "nothing until a page is shared with it."
    ),
    "Slack": (
        "Coming soon — this connector is not available yet. The setup below is "
        "what it will ask for once it ships.\n"
        "\n"
        "Read and post Slack messages, and list channels and users.\n"
        "Requires a `slack` credential with fields: token, teamId.\n"
        "Create an app at api.slack.com/apps, install it to the workspace, and "
        "copy the Bot User OAuth Token (xoxb-…). The workspace ID (T…) is in "
        "Settings → About this workspace."
    ),
}

# The marker the long text carries and the short text never will: credential
# field names in backticks. Matching on it (rather than equality) keeps the
# migration idempotent and leaves an already-trimmed row alone.
JARGON_MARKER = "Requires a `"


def trim_notes(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    for name, short in SHORT_NOTES.items():
        server = MCPServer.objects.filter(name=name, user__isnull=True).first()
        if server is None:
            continue
        if server.setup_notes and JARGON_MARKER in server.setup_notes:
            server.setup_notes = short
            server.save(update_fields=["setup_notes"])


def restore_notes(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    for name, long in LONG_NOTES.items():
        server = MCPServer.objects.filter(name=name, user__isnull=True).first()
        if server is None:
            continue
        if server.setup_notes == SHORT_NOTES[name]:
            server.setup_notes = long
            server.save(update_fields=["setup_notes"])


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0019_native_google_connectors'),
    ]

    operations = [
        migrations.RunPython(trim_notes, restore_notes),
    ]
