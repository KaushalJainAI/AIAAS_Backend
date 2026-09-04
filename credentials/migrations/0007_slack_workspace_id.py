"""
Data migration: give the `slack` credential type its workspace id field.

The Slack MCP server exits during startup unless *both* variables are present:

    Please set SLACK_BOT_TOKEN and SLACK_TEAM_ID environment variables

The catalogue only ever mapped the token, and the credential type only ever
offered a token field, so the Slack connection could not start no matter what
the user entered — and, because the failure happened before the MCP handshake,
it surfaced as a bare 502 with no message rather than as a missing field.

`credentials.0005` seeds from `seed_connector_credentials.CREDENTIAL_TYPES`, so
a *fresh* database already picks the new field up. This migration is for the
databases that ran 0005 before the field existed.
"""
from django.db import migrations


def add_workspace_id(apps, schema_editor):
    CredentialType = apps.get_model('credentials', 'CredentialType')
    slack = CredentialType.objects.filter(slug='slack').first()
    if slack is None:
        return  # 0005 will seed it complete.

    fields = list(slack.fields_schema or [])
    if any(f.get('name') == 'teamId' for f in fields):
        return

    fields.append({
        'name': 'teamId',
        'label': 'Workspace ID',
        'type': 'text',
        'required': True,
        'placeholder': 'T01234567',
    })
    slack.fields_schema = fields
    slack.save(update_fields=['fields_schema'])


def remove_workspace_id(apps, schema_editor):
    CredentialType = apps.get_model('credentials', 'CredentialType')
    slack = CredentialType.objects.filter(slug='slack').first()
    if slack is None:
        return
    slack.fields_schema = [
        f for f in (slack.fields_schema or []) if f.get('name') != 'teamId'
    ]
    slack.save(update_fields=['fields_schema'])


class Migration(migrations.Migration):

    dependencies = [
        ('credentials', '0006_credentialauditlog_snapshot'),
    ]

    operations = [
        migrations.RunPython(add_workspace_id, reverse_code=remove_workspace_id),
    ]
