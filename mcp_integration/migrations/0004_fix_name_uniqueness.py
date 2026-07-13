from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mcp_integration', '0003_rename_mcp_integra_enabled_c3a5b9_idx_mcp_integra_enabled_3bb563_idx'),
    ]

    operations = [
        # Drop the global unique constraint on name ...
        migrations.AlterField(
            model_name='mcpserver',
            name='name',
            field=models.CharField(help_text='Human-readable name for this server', max_length=255),
        ),
        # ... and replace it with a per-user unique constraint.
        # NULL user (system servers) is not enforced by the DB for most backends,
        # but system servers are only created via migrations/admin so that is fine.
        migrations.AlterUniqueTogether(
            name='mcpserver',
            unique_together={('name', 'user')},
        ),
    ]
