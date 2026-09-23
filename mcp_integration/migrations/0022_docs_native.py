"""
Serve Google Docs natively.

The Docs card is stdio with no working server package behind it, so it sat
on "Unavailable" while Drive could only export documents as flat text. The
Docs API needs nothing but the `documents` scope — which the frontend's scope
map already reserves for this card (`lib/googleScopes.ts`, "kept so the row
still resolves if one lands") — so `chat/tools/google/docs.py` reads
documents structurally (headings, lists, tables) and creates/appends through
`documents.create`/`batchUpdate` from this process. The row becomes
`type='native'`; the Connections switch, the `google-oauth2` credential and
agents' `connectors` scopes are keyed on it and keep working. Its stored tool
catalogue is dropped, as with every native conversion.
"""
from django.db import migrations

NOTE = (
    "Read documents with their structure, create new ones, and append text.\n"
    "Click Connect and sign in with Google; the card asks for document access, "
    "kept separate from Drive so existing consents are untouched.\n"
)

# Verbatim pre-image for the reverse, as served on 2026-09-23.
WAS = (
    "Temporarily unavailable. No MCP server for Google Docs has yet been "
    "found that can authenticate without a browser sign-in on the server "
    "itself. Drive can already read Docs files as text, so use that in the "
    "meantime."
)


def _to_native(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPToolCatalogue = apps.get_model("mcp_integration", "MCPToolCatalogue")

    MCPServer.objects.filter(name="Google Docs", user__isnull=True).update(
        type="native",
        command=None,
        args=[],
        url=None,
        env={},
        credential_env_map={},
        credential_header_map={},
        credential_file_map={},
        required_credential_types=["google-oauth2"],
        setup_notes=NOTE,
        enabled=True,
        coming_soon=False,
    )
    MCPToolCatalogue.objects.filter(
        server__user__isnull=True, server__name="Google Docs",
    ).delete()


def _from_native(apps, schema_editor):
    MCPServer = apps.get_model("mcp_integration", "MCPServer")
    MCPServer.objects.filter(name="Google Docs", user__isnull=True).update(
        type="stdio",
        enabled=False,
        setup_notes=WAS,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("mcp_integration", "0021_notion_native"),
    ]

    operations = [
        migrations.RunPython(_to_native, reverse_code=_from_native),
    ]
