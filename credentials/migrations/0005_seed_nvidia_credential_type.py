"""Seed the missing NVIDIA NIM credential type.

The `NvidiaNode` (nodes/handlers/llm_nodes.py) references credential_type="nvidia",
but no prior migration creates the matching CredentialType row. Users hit a 400
when trying to create an NVIDIA credential via the API. This migration adds it.
"""
from django.db import migrations


NVIDIA_DEFAULTS = {
    "name": "NVIDIA NIM API",
    "service_identifier": "nvidia",
    "description": "NVIDIA NIM API key (OpenAI-compatible endpoint).",
    "auth_method": "api_key",
    "fields_schema": [
        {"name": "api_key", "type": "password", "label": "API Key", "required": True},
    ],
    "oauth_config": {},
    "is_active": True,
}


def seed_nvidia(apps, schema_editor):
    CredentialType = apps.get_model("credentials", "CredentialType")
    CredentialType.objects.update_or_create(slug="nvidia", defaults=NVIDIA_DEFAULTS)


def unseed_nvidia(apps, schema_editor):
    CredentialType = apps.get_model("credentials", "CredentialType")
    CredentialType.objects.filter(slug="nvidia").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("credentials", "add_google_oauth_fields"),
    ]

    operations = [
        migrations.RunPython(seed_nvidia, reverse_code=unseed_nvidia),
    ]
