from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('logs', '0006_executionlog_supervision_level_and_more'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='executionlog',
            index=models.Index(fields=['workflow', '-created_at', '-id'], name='logs_execut_workflo_9d79e3_idx'),
        ),
        migrations.AddIndex(
            model_name='executionlog',
            index=models.Index(fields=['user', '-created_at', '-id'], name='logs_execut_user_id_cb8c02_idx'),
        ),
        migrations.AddIndex(
            model_name='auditentry',
            index=models.Index(fields=['user', '-created_at', '-id'], name='logs_audite_user_id_9cc0ea_idx'),
        ),
    ]
