"""
Data migration: give the `slack` credential type its user token field.

The messaging adapter (`chat/tools/messaging/slack.py`) posts with the bot
token and searches with a user token (`search:read`) — two different
authorities — but the type only ever offered `token` and `teamId`. Search and
read therefore always raised "not connected", no matter what the user stored.

`credentials.0005` seeds from `seed_connector_credentials.CREDENTIAL_TYPES`, so
a *fresh* database already picks the new field up. This migration is for the
databases that ran 0005 before the field existed. Optional, so existing
connections keep validating.
"""
from django.db import migrations


def add_user_token(apps, schema_editor):
    CredentialType = apps.get_model('credentials', 'CredentialType')
    slack = CredentialType.objects.filter(slug='slack').first()
    if slack is None:
        return  # 0005 will seed it complete.

    fields = list(slack.fields_schema or [])
    if any(f.get('name') == 'user_token' for f in fields):
        return

    fields.append({
        'name': 'user_token',
        'label': 'User OAuth Token',
        'type': 'password',
        'required': False,
        'placeholder': 'xoxp-...',
    })
    slack.fields_schema = fields
    slack.save(update_fields=['fields_schema'])


def remove_user_token(apps, schema_editor):
    CredentialType = apps.get_model('credentials', 'CredentialType')
    slack = CredentialType.objects.filter(slug='slack').first()
    if slack is None:
        return
    slack.fields_schema = [
        f for f in (slack.fields_schema or []) if f.get('name') != 'user_token'
    ]
    slack.save(update_fields=['fields_schema'])


class Migration(migrations.Migration):

    dependencies = [
        ('credentials', '0008_google_oauth_config'),
    ]

    operations = [
        migrations.RunPython(add_user_token, reverse_code=remove_user_token),
    ]
