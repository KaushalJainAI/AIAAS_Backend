"""
Data migration: repair credential maps that could never resolve.

Two mismatches meant six of the eleven curated connectors failed at run time on
a connection the UI reported as connected:

  * Notion mapped `notion:api_key`, but the seeded `notion` credential type
    defines its field as `token` — the injector raises
    CredentialInvalidError("missing field 'api_key'").

  * The five Google servers mapped `google-oauth2:client_id` and
    `:client_secret`, but the `google-oauth2` credential type only defines
    `access_token` / `refresh_token`. A user could not supply those fields from
    any UI, and they should not have to: the OAuth client belongs to the
    platform, not the user. They now come from Django settings via the
    `@settings:` source, leaving the user to supply only the refresh token —
    which the "Sign in with Google" flow produces for them.
"""
from django.db import migrations


_GOOGLE_PREFIXES = {
    'Google Drive': 'GDRIVE',
    'Gmail': 'GOOGLE',
    'Google Calendar': 'GOOGLE',
    'Google Sheets': 'GOOGLE',
    'Google Docs': 'GOOGLE',
}


def fix(apps, schema_editor):
    MCPServer = apps.get_model('mcp_integration', 'MCPServer')

    for name, prefix in _GOOGLE_PREFIXES.items():
        for server in MCPServer.objects.filter(name=name, user__isnull=True):
            server.credential_env_map = {
                f'{prefix}_CLIENT_ID': '@settings:GOOGLE_OAUTH_CLIENT_ID',
                f'{prefix}_CLIENT_SECRET': '@settings:GOOGLE_OAUTH_CLIENT_SECRET',
                f'{prefix}_REFRESH_TOKEN': 'google-oauth2:refresh_token',
            }
            server.setup_notes = (
                "Sign in with Google to connect. The platform supplies the OAuth "
                "client; only your account authorisation is stored."
            )
            server.save(update_fields=['credential_env_map', 'setup_notes'])

    for server in MCPServer.objects.filter(name='Notion', user__isnull=True):
        server.credential_env_map = {'NOTION_API_KEY': 'notion:token'}
        server.save(update_fields=['credential_env_map'])


def unfix(apps, schema_editor):
    MCPServer = apps.get_model('mcp_integration', 'MCPServer')

    for name, prefix in _GOOGLE_PREFIXES.items():
        for server in MCPServer.objects.filter(name=name, user__isnull=True):
            server.credential_env_map = {
                f'{prefix}_CLIENT_ID': 'google-oauth2:client_id',
                f'{prefix}_CLIENT_SECRET': 'google-oauth2:client_secret',
                f'{prefix}_REFRESH_TOKEN': 'google-oauth2:refresh_token',
            }
            server.save(update_fields=['credential_env_map'])

    for server in MCPServer.objects.filter(name='Notion', user__isnull=True):
        server.credential_env_map = {'NOTION_API_KEY': 'notion:api_key'}
        server.save(update_fields=['credential_env_map'])


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0009_backfill_connector_presentation'),
    ]

    operations = [
        migrations.RunPython(fix, unfix),
    ]
