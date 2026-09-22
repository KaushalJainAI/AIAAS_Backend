# Coding-team C1: resolved project commands per class.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0002_codechange_codeproject'),
    ]

    operations = [
        migrations.AddField(
            model_name='codeproject',
            name='commands',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
