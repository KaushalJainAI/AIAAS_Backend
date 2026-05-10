"""
Add manual-entry fields to the google-oauth2 CredentialType so users can
connect Google services by pasting their OAuth credentials rather than going
through a redirect flow.

Fields added: client_id, client_secret, refresh_token
These map to the env vars injected into Google MCP servers.
"""
from django.db import migrations


_FIELDS = [
    {
        "name": "client_id",
        "label": "Client ID",
        "type": "text",
        "required": True,
        "placeholder": "GXXXXXX-xxxx.apps.googleusercontent.com",
    },
    {
        "name": "client_secret",
        "label": "Client Secret",
        "type": "password",
        "required": True,
        "placeholder": "GOCSPX-...",
    },
    {
        "name": "refresh_token",
        "label": "Refresh Token",
        "type": "password",
        "required": True,
        "placeholder": "1//...",
    },
]


def _forward(apps, schema_editor):
    CredentialType = apps.get_model("credentials", "CredentialType")
    try:
        ct = CredentialType.objects.get(slug="google-oauth2")
        if not ct.fields_schema:
            ct.fields_schema = _FIELDS
            ct.save(update_fields=["fields_schema"])
    except CredentialType.DoesNotExist:
        pass


def _reverse(apps, schema_editor):
    CredentialType = apps.get_model("credentials", "CredentialType")
    try:
        ct = CredentialType.objects.get(slug="google-oauth2")
        ct.fields_schema = []
        ct.save(update_fields=["fields_schema"])
    except CredentialType.DoesNotExist:
        pass


class Migration(migrations.Migration):

    dependencies = [
        # Point to the last migration in the credentials app
        ('credentials', '0004_credentialtype_service_identifier'),
    ]

    operations = [
        migrations.RunPython(_forward, reverse_code=_reverse),
    ]
