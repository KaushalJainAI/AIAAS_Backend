"""Serve Google connectors natively and retire the no-credential utility rows.

Every curated connector was an `npx` MCP server — a Node process of 70-150 MB
— on a production box whose web container has 384 MB. That is what killed
daphne on 2026-09-16, and the memory budget added in response meant the tool
lists were never built, so the connectors silently never appeared in chat.

**Google (Gmail, Drive, Sheets, Calendar) becomes `type='native'`.** Their
tools now live in `chat/tools/google/` and call Google's REST APIs from this
process. The rows stay, because the Connections switch, the `google-oauth2`
credential they require and every agent's `connectors` scope are keyed on
them; only the process wiring is cleared, since nothing reads it any more and a
leftover `command` on a native row would be a claim that something starts.

**Filesystem, Fetch, Memory and Sequential Thinking are switched off.** Each is
a Node process duplicating something built in — the user's own files
(`inference/vfs.py`), `read_url`, user memory (`core/memory.py`), and the
model's own reasoning. Filesystem pointed at `/tmp` *on the server*, which was
never the user's files. Disabled rather than deleted, with a note saying where
the capability went, so `statusOf` renders an explanation rather than a gap.

Their stored tool catalogues are dropped: a catalogue row for a native server
would be a second, stale answer to a question the registry now owns.

Reversing restores the previous process wiring from `0012` and `0014` rather
than a copy of it, so the two cannot disagree.
"""

import importlib

from django.db import migrations, models

NATIVE = ("Gmail", "Google Drive", "Google Sheets", "Google Calendar")

_GOOGLE_NOTE_TAIL = (
    "Click Connect and sign in with Google; nothing needs to be copied by "
    "hand. One Google connection serves Gmail, Drive, Sheets and Calendar — "
    "each card asks only for the permissions it needs.\n"
    "If it stops working after about a week, reconnect: Google expires "
    "refresh tokens for OAuth apps that are still in Testing status."
)

NATIVE_NOTES = {
    "Gmail": "Search, read, draft, send and label email, and move messages to Trash.\n",
    "Google Drive": "Find files and read Docs, Slides, PDFs and Word files; create new files.\n",
    "Google Sheets": "Read ranges from a spreadsheet and write values into it.\n",
    "Google Calendar": (
        "List and search events, check free/busy, create, update and cancel "
        "events, and answer invitations.\n"
    ),
}

RETIRED = {
    "Filesystem": (
        "Built in now. Agents and chat read and write your own files (the Files "
        "page) without this connector, so it has been switched off."
    ),
    "Fetch": (
        "Built in now. Reading a web page is a standard tool, so this connector "
        "has been switched off."
    ),
    "Memory": (
        "Built in now. Chat remembers facts about you across conversations "
        "without this connector, so it has been switched off."
    ),
    "Sequential Thinking": (
        "Built in now. Current models reason step by step on their own, so this "
        "connector has been switched off."
    ),
}


def _to_native(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPToolCatalogue = apps.get_model("mcp_integration", "MCPToolCatalogue")

    for name in NATIVE:
        MCPServer.objects.filter(name=name, user__isnull=True).update(
            type="native",
            command=None,
            args=[],
            url=None,
            env={},
            credential_env_map={},
            credential_header_map={},
            credential_file_map={},
            required_credential_types=["google-oauth2"],
            setup_notes=NATIVE_NOTES[name] + _GOOGLE_NOTE_TAIL,
            enabled=True,
        )

    for name, note in RETIRED.items():
        MCPServer.objects.filter(name=name, user__isnull=True).update(
            enabled=False, setup_notes=note,
        )

    MCPToolCatalogue.objects.filter(
        server__user__isnull=True,
        server__name__in=(*NATIVE, *RETIRED),
    ).delete()


def _from_native(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")

    gmail = importlib.import_module(
        "mcp_integration.migrations.0012_enable_gmail_connector"
    ).GMAIL
    files = importlib.import_module(
        "mcp_integration.migrations.0014_enable_google_file_connectors"
    )._ROWS
    for name, fields in {"Gmail": gmail, **files}.items():
        MCPServer.objects.filter(name=name, user__isnull=True).update(type="stdio", **fields)

    seeded = importlib.import_module(
        "mcp_integration.migrations.0005_seed_curated_mcp_servers"
    )
    rows = {row["name"]: row for row in seeded._CURATED_SERVERS}
    for name in RETIRED:
        row = rows.get(name) or {}
        MCPServer.objects.filter(name=name, user__isnull=True).update(
            enabled=True, setup_notes=row.get("setup_notes", ""),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("mcp_integration", "0018_mcptoolcatalogue"),
    ]

    operations = [
        migrations.AlterField(
            model_name="mcpserver",
            name="type",
            field=models.CharField(
                choices=[
                    ("stdio", "Standard Input/Output (Subprocess)"),
                    ("http", "Streamable HTTP"),
                    ("sse", "Server-Sent Events (HTTP, deprecated)"),
                    ("native", "Native (built-in tools)"),
                ],
                default="stdio",
                max_length=10,
            ),
        ),
        migrations.RunPython(_to_native, _from_native),
    ]
