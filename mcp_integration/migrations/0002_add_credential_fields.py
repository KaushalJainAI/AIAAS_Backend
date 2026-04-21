from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='mcpserver',
            name='required_credential_types',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="List of CredentialType slugs this server requires (e.g., ['github_token'])",
            ),
        ),
        migrations.AddField(
            model_name='mcpserver',
            name='credential_env_map',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Maps env var name -> "<credential_slug>:<field>". Used for stdio.',
            ),
        ),
        migrations.AddField(
            model_name='mcpserver',
            name='credential_header_map',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Maps HTTP header name -> value (may contain {slug:field} placeholders). Used for SSE.',
            ),
        ),
        migrations.AddField(
            model_name='mcpserver',
            name='setup_notes',
            field=models.TextField(blank=True, help_text='Human-readable setup notes shown in the UI'),
        ),
        migrations.AlterField(
            model_name='mcpserver',
            name='env',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Non-secret environment variables to pass to the server',
            ),
        ),
        migrations.AddIndex(
            model_name='mcpserver',
            index=models.Index(fields=['enabled', 'user'], name='mcp_integra_enabled_c3a5b9_idx'),
        ),
    ]
