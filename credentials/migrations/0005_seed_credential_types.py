"""
Data migration: seed the credential types that nodes and MCP connectors reference.

These rows were only ever created by `manage.py seed_connector_credentials`, while
the curated MCP servers that depend on them are created by a migration
(`mcp_integration.0005`). A deploy that ran `migrate` but not the command therefore
came up with a full connector catalog and no credential types behind it: the
credential picker on a node rendered an empty dropdown, and every credentialed
connection on the Connections page was permanently stuck at "Not connected"
because `credTypeBySlug.get(slug)` found nothing. Nothing logged an error.

Seeding here makes `migrate` sufficient on its own. The command stays — it is
still the way to re-seed after editing the catalog, and it remains the single
source of truth for the data, which this migration imports rather than copies.
"""
from django.db import migrations


# Only fields that existed when this migration was written. Importing the spec
# list gives one source of truth; filtering the keys keeps a replay on a fresh
# database working even if the model later gains or loses a column.
_WRITABLE = {
    'name', 'service_identifier', 'auth_method', 'description',
    'icon', 'fields_schema', 'is_active',
}


def seed(apps, schema_editor):
    CredentialType = apps.get_model('credentials', 'CredentialType')

    # Imported from the command module, which holds only plain data — no model
    # imports — so this stays safe to run under a historical model state.
    from credentials.management.commands.seed_connector_credentials import (
        CREDENTIAL_TYPES,
    )

    model_fields = {f.name for f in CredentialType._meta.get_fields()}
    allowed = _WRITABLE & model_fields

    for spec in CREDENTIAL_TYPES:
        defaults = {
            'name': spec['name'],
            # Mirrors the slug: the frontend resolves a node's `credentialType`
            # against this column to narrow the credential picker.
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


def unseed(apps, schema_editor):
    """
    Deliberately a no-op.

    Reversing would delete credential types that users' stored Credential rows
    point at, cascading away their saved secrets to undo a seed. Leaving the rows
    in place is the safe direction; they are inert if unused.
    """


class Migration(migrations.Migration):

    dependencies = [
        # Depends on `add_google_oauth_fields` rather than 0004: that oddly-named
        # migration is the app's other leaf, and depending on it keeps a single
        # leaf in the graph instead of requiring a merge migration.
        ('credentials', 'add_google_oauth_fields'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
