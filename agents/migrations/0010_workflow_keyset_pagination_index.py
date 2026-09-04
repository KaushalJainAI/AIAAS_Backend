from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orchestrator', '0009_alter_workflow_supervision_level'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='workflow',
            index=models.Index(fields=['user', '-updated_at', '-id'], name='orchestrato_user_id_8510f4_idx'),
        ),
    ]
