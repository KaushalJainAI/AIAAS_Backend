"""
Data migration: seed the `opencode` (OpenCode Zen) credential type.

`credentials.0005` seeds from `seed_connector_credentials.CREDENTIAL_TYPES`,
so a *fresh* database already picks this up. This migration is for the
databases that ran 0005 before the type existed. Same dict, imported from the
seed command rather than copied (as 0005 does — import inside the function,
never at module scope). Reverse deletes the row by slug; user-held `opencode`
credentials cannot exist yet, since the type is what makes them creatable.
"""
from django.db import migrations


_WRITABLE = {
    'name', 'service_identifier', 'auth_method', 'description',
    'icon', 'fields_schema', 'is_active',
}


def seed_opencode(apps, schema_editor):
    CredentialType = apps.get_model('credentials', 'CredentialType')
    from credentials.management.commands.seed_connector_credentials import (
        CREDENTIAL_TYPES,
    )

    spec = next(s for s in CREDENTIAL_TYPES if s['slug'] == 'opencode')
    model_fields = {f.name for f in CredentialType._meta.get_fields()}
    allowed = _WRITABLE & model_fields
    defaults = {
        'name': spec['name'],
        'service_identifier': spec.get('service_identifier', spec['slug']),
        'auth_method': spec.get('auth_method', 'api_key'),
        'description': spec.get('description', ''),
        'icon': spec.get('icon', 'Key'),
        'fields_schema': spec.get('fields_schema', []),
        'is_active': True,
    }
    CredentialType.objects.update_or_create(
        slug=spec['slug'],
        defaults={k: v for k, v in defaults.items() if k in allowed},
    )


def unseed_opencode(apps, schema_editor):
    CredentialType = apps.get_model('credentials', 'CredentialType')
    CredentialType.objects.filter(slug='opencode').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('credentials', '0009_slack_user_token'),
    ]

    operations = [
        migrations.RunPython(seed_opencode, reverse_code=unseed_opencode),
    ]
