"""
Data migration: Gmail works, and it always could have.

`0011` disabled the five Google rows on the finding that every available server
"authenticates by opening a browser against an OAuth keys file on the server's
own disk". For `@shinzolabs/gmail-mcp` that finding was wrong, and the way it
was reached is worth writing down because it will otherwise be repeated: the
package was tested by *running it* and observing that it hung. A stdio MCP
server always hangs when run bare — it is waiting for JSON-RPC on stdin. The
measurement could not have distinguished a working connector from a broken one.

Re-measured with an actual MCP handshake (initialize + tools/list), with only
`CLIENT_ID`, `CLIENT_SECRET` and `REFRESH_TOKEN` in the environment:
**64 tools, no browser, no file on disk.** `dist/oauth2.js` resolves env-based
credentials first and only starts its interactive auth server when invoked as
`gmail-mcp auth` — a code path we never take.

So this row needs no new mechanism. The three values are exactly what the
injector already produces: two `@settings` reads for the platform's OAuth client
and one per-user refresh token out of the vault.

The other four rows stay disabled and are *not* covered by this:
  * Drive and Sheets (`@isaacphi/mcp-gdrive`) and Calendar
    (`@cocal/google-calendar-mcp`) both verified working, but each needs a
    credential file written to disk, which the injector cannot yet do.
  * Docs has no verified package at all.
Their notes are rewritten to say which of those two situations they are in,
because "temporarily unavailable" told a user nothing and told the next
maintainer something false.
"""
from django.db import migrations


GMAIL = {
    "command": "npx",
    "args": ["-y", "@shinzolabs/gmail-mcp"],
    # The package defaults telemetry to on (`process.env.TELEMETRY_ENABLED ||
    # "true"`). A connector that phones home about a user's mailbox activity is
    # not a default we get to make on their behalf.
    "env": {"TELEMETRY_ENABLED": "false"},
    "required_credential_types": ["google-oauth2"],
    "credential_env_map": {
        "CLIENT_ID": "@settings:GOOGLE_OAUTH_CLIENT_ID",
        "CLIENT_SECRET": "@settings:GOOGLE_OAUTH_CLIENT_SECRET",
        "REFRESH_TOKEN": "google-oauth2:refresh_token",
    },
    "setup_notes": (
        "Read, search, send, and organise Gmail — messages, threads, labels, "
        "filters, and drafts.\n"
        "Click Connect and sign in with Google; nothing needs to be copied by "
        "hand. The connection uses this platform's OAuth client, so you do not "
        "need a Google Cloud project of your own.\n"
        "If it stops working after about a week, reconnect: Google expires "
        "refresh tokens for OAuth apps that are still in Testing status."
    ),
    "enabled": True,
}

# Verified working, but blocked on credential-file materialisation.
_BLOCKED_ON_FILES = (
    "Temporarily unavailable. The server for this connection has been verified "
    "to work, but it reads its credentials from a file on disk rather than from "
    "the environment, and this platform can only inject environment variables "
    "so far. It will be enabled once that lands — no action is needed from you."
)

# No package known to work at all.
_NO_SERVER = (
    "Temporarily unavailable. No MCP server for this service has yet been found "
    "that can authenticate from an environment variable, which is the only way "
    "this platform can pass your credentials to one."
)

_NOTES = {
    "Google Drive": _BLOCKED_ON_FILES,
    "Google Sheets": _BLOCKED_ON_FILES,
    "Google Calendar": _BLOCKED_ON_FILES,
    "Google Docs": _NO_SERVER,
}


def _apply(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    # Curated rows only: a user who made their own "Gmail" owns that config.
    MCPServer.objects.filter(name="Gmail", user__isnull=True).update(**GMAIL)
    for name, note in _NOTES.items():
        MCPServer.objects.filter(name=name, user__isnull=True).update(
            enabled=False, setup_notes=note,
        )


def _reverse(apps, schema_editor):
    """Turn Gmail back off, without restoring the package that never worked."""
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPServer.objects.filter(name="Gmail", user__isnull=True).update(
        enabled=False,
        setup_notes=_BLOCKED_ON_FILES,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("mcp_integration", "0011_working_connector_catalogue"),
    ]

    operations = [
        migrations.RunPython(_apply, reverse_code=_reverse),
    ]
