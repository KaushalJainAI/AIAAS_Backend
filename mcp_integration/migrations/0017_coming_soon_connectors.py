"""Mark Notion and Slack as upcoming features rather than connectable ones.

Both rows were `enabled=True` and shipped a working npm package, so the
Connections page offered a Connect button that started a real credential flow
for a connector the product is not ready to support. `enabled=False` is what
actually withdraws them -- `_visible_servers_queryset` filters on it, so the
tools leave every agent's toolbox, and the toggle endpoint answers 409 rather
than storing a preference the platform would override.

`coming_soon` is added alongside because `enabled=False` alone renders as
"Unavailable", which is the wording reserved for something broken (Google
Workspace, whose browser-based auth this platform cannot drive). "Coming soon"
and "Unavailable" need opposite readings, and the UI must not tell them apart
by connector name -- connector metadata is data, not code.

The flag grants nothing on its own: it is presentation only, and every read
path still gates on `enabled`.

Reversing restores both rows exactly, including the setup notes, by stripping
the one line this migration prepends.
"""

from django.db import migrations, models

# Curated rows only. A user's own server that happens to be named "Notion" is
# theirs, and nothing here is entitled to switch it off.
UPCOMING = ("Notion", "Slack")

NOTE_PREFIX = (
    "Coming soon — this connector is not available yet. The setup below is "
    "what it will ask for once it ships.\n\n"
)


def mark_upcoming(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    for server in MCPServer.objects.filter(name__in=UPCOMING, user__isnull=True):
        server.enabled = False
        server.coming_soon = True
        # Idempotent: a re-run (or a squash replaying this) must not stack
        # the prefix, and the reverse below matches on it exactly.
        if not server.setup_notes.startswith(NOTE_PREFIX):
            server.setup_notes = NOTE_PREFIX + server.setup_notes
        server.save(update_fields=["enabled", "coming_soon", "setup_notes"])


def unmark_upcoming(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    for server in MCPServer.objects.filter(name__in=UPCOMING, user__isnull=True):
        server.enabled = True
        server.coming_soon = False
        if server.setup_notes.startswith(NOTE_PREFIX):
            server.setup_notes = server.setup_notes[len(NOTE_PREFIX):]
        server.save(update_fields=["enabled", "coming_soon", "setup_notes"])


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0016_mcpoauthclient_mcpoauthflow_mcpoauthtoken'),
    ]

    operations = [
        migrations.AddField(
            model_name='mcpserver',
            name='coming_soon',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Show as an upcoming feature. Pair with enabled=False; '
                    'this flag alone grants nothing.'
                ),
            ),
        ),
        migrations.RunPython(mark_upcoming, unmark_upcoming),
    ]
