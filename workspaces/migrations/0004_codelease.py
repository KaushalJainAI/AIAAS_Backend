# Coding-team C2: file leases.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0003_codeproject_commands'),
    ]

    operations = [
        migrations.CreateModel(
            name='CodeLease',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('pattern', models.CharField(help_text='Normalised path or dir/** subtree, relative to the project root.', max_length=500)),
                ('holder_label', models.CharField(blank=True, default='', max_length=120)),
                ('mode', models.CharField(default='write', max_length=12)),
                ('task_id', models.CharField(blank=True, default='', max_length=64)),
                ('acquired_at', models.DateTimeField(auto_now_add=True)),
                ('heartbeat_at', models.DateTimeField(auto_now=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('holder', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='code_leases', to='logs.executionlog')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='leases', to='workspaces.codeproject')),
            ],
            options={
                'ordering': ['-acquired_at'],
                'indexes': [models.Index(fields=['project', 'expires_at'], name='workspaces__project_dd9fe2_idx'),
                            models.Index(fields=['holder', 'project'], name='workspaces__holder__9309d8_idx')],
            },
        ),
    ]
