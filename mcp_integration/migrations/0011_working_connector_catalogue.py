"""
Data migration: make every *enabled* connector one that actually starts.

`mcp_integration.0005` seeded eleven curated servers. Six named npm packages
that do not exist, and three more could not start for reasons the catalogue
could not express. Nothing caught it: the endpoint that would have reported the
failure timed out before npm finished saying "404", so the whole catalogue
returned the same bare 502 whether a connector was healthy or fictional.

Every package below was verified by starting it and reading back its tool list.

Repointed
---------
Fetch   `@modelcontextprotocol/server-fetch` was never published — the official
        fetch server is Python-only, and this image has node, not uv. Replaced
        with `@tokenizin/mcp-npx-fetch` (4 tools, no credentials).
Notion  `@modelcontextprotocol/server-notion` was never published either.
        Replaced with Notion's own `@notionhq/notion-mcp-server` (24 tools),
        which reads `NOTION_TOKEN` rather than `NOTION_API_KEY`.
Slack   The package was fine; the mapping was short one variable. The server
        exits at startup with "Please set SLACK_BOT_TOKEN and SLACK_TEAM_ID",
        so a workspace id is now mapped from the credential (see
        `credentials.0007`).

Disabled
--------
Google Drive, Gmail, Calendar, Sheets, Docs. `@gptscript-ai/google-workspace-mcp`
does not exist, and `@modelcontextprotocol/server-gdrive` is deprecated and
hangs. The available replacements all authenticate by running an interactive
browser flow against a `gcp-oauth.keys.json` file on disk — measured directly:
`@cocal/google-calendar-mcp` blocks for ever, `@gongrzhe/server-gmail-autoauth-mcp`
exits with "OAuth keys file not found", `@shinzolabs/gmail-mcp` blocks for ever.
None of them can be driven by the env-var credential injection this platform
has, so none can be made to work by editing a row.

They are disabled rather than deleted: the rows carry the credential wiring and
the presentation, and the work needed is a connector that accepts a refresh
token from the environment — not a rediscovery of which packages are broken.

Note that `enabled=False` on a curated row is absolute, not a default. A user
preference can only ever take a connection *away* (`_visible_servers_queryset`
filters `enabled=True` and then subtracts the user's "off" choices), so nobody
can switch these back on from the UI. That is the intended reading: the row is
a placeholder for work not yet done, not a connection someone might get lucky
with. Reviving one means editing the row.
"""
from django.db import migrations


# name -> the fields to overwrite.
_REPOINTED = {
    "Fetch": {
        "command": "npx",
        "args": ["-y", "@tokenizin/mcp-npx-fetch"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "setup_notes": (
            "Fetch a public web page as HTML, Markdown, plain text, or JSON. "
            "No credentials required.\n"
            "Note: this connector reaches any URL the agent asks for, without "
            "the private-address checks the built-in `read_url` tool applies. "
            "Prefer `read_url` unless you specifically need Markdown or JSON "
            "extraction, and turn this off on a host with link-local metadata "
            "endpoints you care about."
        ),
        "enabled": True,
    },
    "Notion": {
        "command": "npx",
        "args": ["-y", "@notionhq/notion-mcp-server"],
        "env": {},
        "required_credential_types": ["notion"],
        # The official server reads NOTION_TOKEN; NOTION_API_KEY is ignored.
        "credential_env_map": {"NOTION_TOKEN": "notion:token"},
        "setup_notes": (
            "Search, read, and update Notion pages and databases.\n"
            "Requires a `notion` credential with field: token.\n"
            "Create an internal integration at notion.so/my-integrations, then "
            "share the target pages or databases with that integration — a "
            "token alone sees nothing until a page is shared with it."
        ),
        "enabled": True,
    },
    "Slack": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env": {},
        "required_credential_types": ["slack"],
        "credential_env_map": {
            "SLACK_BOT_TOKEN": "slack:token",
            # Without this the server exits before the MCP handshake.
            "SLACK_TEAM_ID": "slack:teamId",
        },
        "setup_notes": (
            "Read and post Slack messages, and list channels and users.\n"
            "Requires a `slack` credential with fields: token, teamId.\n"
            "Create an app at api.slack.com/apps, install it to the workspace, "
            "and copy the Bot User OAuth Token (xoxb-…). The workspace ID "
            "(T…) is in Settings → About this workspace."
        ),
        "enabled": True,
    },
}

_DISABLED_NOTE = (
    "Temporarily unavailable. The MCP servers for Google Workspace all "
    "authenticate by opening a browser against an OAuth keys file on the "
    "server's own disk, which this platform's credential injection cannot "
    "drive. Enabling this row will not make it connect."
)

_DISABLED = [
    "Google Drive", "Gmail", "Google Calendar", "Google Sheets", "Google Docs",
]


def _fix(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")

    for name, fields in _REPOINTED.items():
        # Curated rows only (`user IS NULL`): a user who created their own
        # server called "Fetch" owns that config, and this migration does not.
        MCPServer.objects.filter(name=name, user__isnull=True).update(**fields)

    for name in _DISABLED:
        MCPServer.objects.filter(name=name, user__isnull=True).update(
            enabled=False, setup_notes=_DISABLED_NOTE,
        )


def _noop_reverse(apps, schema_editor):
    """
    Deliberately not reversible.

    Every value this replaced named a package that does not exist or a server
    that cannot start, so restoring it would only reintroduce the outage.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0010_fix_broken_credential_maps'),
        ('credentials', '0007_slack_workspace_id'),
    ]

    operations = [
        migrations.RunPython(_fix, reverse_code=_noop_reverse),
    ]
