from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orchestrator', '0023_alter_sharedagent_visibility'),
    ]

    operations = [
        migrations.AddField(
            model_name='subagent',
            name='template_slug',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
