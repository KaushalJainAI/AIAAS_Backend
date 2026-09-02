"""
Data migration: Drive, Sheets and Calendar, now that credential files exist.

`0012` enabled Gmail because `@shinzolabs/gmail-mcp` reads its credentials
straight from the environment. These three do not — they read a file — which is
the only reason they stayed off. `credential_file_map` (migration `0013`) closes
that: the injector renders a template with the same `{slug:field}` placeholders
a header uses, and `_SessionWorker` writes it to a 0700 directory it creates and
removes in its own task.

Verified by handshake (initialize + tools/list) on 2026-08-31:

  `@isaacphi/mcp-gdrive`         4 tools — gdrive_search, gdrive_read_file,
                                 gsheets_read, gsheets_update_cell
  `@cocal/google-calendar-mcp`  13 tools

Drive and Sheets are the **same package**: it carries both the Drive and the
Sheets tools, so the two rows point at one spec and differ only in presentation
and OAuth scope. They are kept as two rows because a user who wants spreadsheets
should not have to know that the Drive connector is where they live, and because
turning one off should not withdraw the other.

Google Docs stays disabled. No package has been found that authenticates from
anything but an interactive browser flow, so it is a different problem from
these three and its note says so.

One wrinkle worth recording: `@isaacphi/mcp-gdrive` writes `Starting server` to
stdout *before* the MCP handshake, which is not JSON-RPC. Our client logs a
parse error for that line and carries on — harmless, but it looks alarming in
logs and is not a symptom of anything.
"""
from django.db import migrations


_GOOGLE_CLIENT = {
    "CLIENT_ID": "@settings:GOOGLE_OAUTH_CLIENT_ID",
    "CLIENT_SECRET": "@settings:GOOGLE_OAUTH_CLIENT_SECRET",
}

# `@isaacphi/mcp-gdrive` reads CLIENT_ID/CLIENT_SECRET from the environment but
# expects the token in a file it names itself, inside GDRIVE_CREDS_DIR — hence
# target="dir".
_GDRIVE_FILES = {
    "GDRIVE_CREDS_DIR": {
        "filename": ".gdrive-server-credentials.json",
        "target": "dir",
        "content": {"refresh_token": "{google-oauth2:refresh_token}"},
    },
}

# The calendar server wants two files and is given the path to each.
# `tokens.json` is keyed by account mode; "normal" is the default mode, and the
# key must match or the server reports no valid tokens.
_CALENDAR_FILES = {
    "GOOGLE_OAUTH_CREDENTIALS": {
        "filename": "gcp-oauth.keys.json",
        "target": "file",
        "content": {
            "installed": {
                "client_id": "{@settings:GOOGLE_OAUTH_CLIENT_ID}",
                "client_secret": "{@settings:GOOGLE_OAUTH_CLIENT_SECRET}",
                "redirect_uris": ["http://localhost:3000/oauth2callback"],
            }
        },
    },
    "GOOGLE_CALENDAR_MCP_TOKEN_PATH": {
        "filename": "tokens.json",
        "target": "file",
        "content": {
            "normal": {
                "refresh_token": "{google-oauth2:refresh_token}",
                "access_token": "{google-oauth2:access_token}",
                "token_type": "Bearer",
                # Deliberately in the past: the server then refreshes on its
                # first call rather than trusting an access token that has been
                # sitting in our database. Claiming a far-future expiry would
                # make it use a stale token and fail the first request instead.
                "expiry_date": 1,
            }
        },
    },
}

_ROWS = {
    "Google Drive": {
        "command": "npx",
        "args": ["-y", "@isaacphi/mcp-gdrive"],
        "env": {},
        "required_credential_types": ["google-oauth2"],
        "credential_env_map": dict(_GOOGLE_CLIENT),
        "credential_file_map": _GDRIVE_FILES,
        "setup_notes": (
            "Search and read files in Google Drive.\n"
            "Click Connect and sign in with Google; nothing needs to be copied "
            "by hand.\n"
            "If it stops working after about a week, reconnect: Google expires "
            "refresh tokens for OAuth apps still in Testing status."
        ),
        "enabled": True,
    },
    "Google Sheets": {
        "command": "npx",
        "args": ["-y", "@isaacphi/mcp-gdrive"],
        "env": {},
        "required_credential_types": ["google-oauth2"],
        "credential_env_map": dict(_GOOGLE_CLIENT),
        "credential_file_map": _GDRIVE_FILES,
        "setup_notes": (
            "Read spreadsheet ranges and update individual cells.\n"
            "Click Connect and sign in with Google. This uses the same Google "
            "connection as Drive, so connecting either one connects both.\n"
            "If it stops working after about a week, reconnect: Google expires "
            "refresh tokens for OAuth apps still in Testing status."
        ),
        "enabled": True,
    },
    "Google Calendar": {
        "command": "npx",
        "args": ["-y", "@cocal/google-calendar-mcp"],
        "env": {},
        "required_credential_types": ["google-oauth2"],
        "credential_env_map": {},
        "credential_file_map": _CALENDAR_FILES,
        "setup_notes": (
            "List, search, create and update calendar events, and check "
            "free/busy times.\n"
            "Click Connect and sign in with Google; nothing needs to be copied "
            "by hand.\n"
            "If it stops working after about a week, reconnect: Google expires "
            "refresh tokens for OAuth apps still in Testing status."
        ),
        "enabled": True,
    },
}

_DOCS_NOTE = (
    "Temporarily unavailable. No MCP server for Google Docs has yet been found "
    "that can authenticate without a browser sign-in on the server itself. "
    "Drive can already read Docs files as text, so use that in the meantime."
)


def _apply(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    for name, fields in _ROWS.items():
        MCPServer.objects.filter(name=name, user__isnull=True).update(**fields)
    MCPServer.objects.filter(name="Google Docs", user__isnull=True).update(
        enabled=False, setup_notes=_DOCS_NOTE,
    )


def _reverse(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPServer.objects.filter(
        name__in=list(_ROWS), user__isnull=True
    ).update(enabled=False, credential_file_map={})


class Migration(migrations.Migration):

    dependencies = [
        ("mcp_integration", "0013_credential_file_map"),
    ]

    operations = [
        migrations.RunPython(_apply, reverse_code=_reverse),
    ]
