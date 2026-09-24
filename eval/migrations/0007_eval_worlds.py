# Generated for EVAL_ENVIRONMENTS_PLAN E-1: fake worlds a suite's cases share.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('eval', '0006_eval_intents'),
    ]

    operations = [
        migrations.CreateModel(
            name='EvalWorld',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.PositiveIntegerField(default=1)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('accepted', 'Accepted')], default='draft', max_length=8)),
                ('brief', models.TextField(blank=True)),
                ('surfaces', models.JSONField(blank=True, default=dict)),
                ('fixtures', models.JSONField(blank=True, default=dict)),
                ('facts', models.JSONField(blank=True, default=list)),
                ('created_by_model', models.CharField(blank=True, max_length=200)),
                ('cost_usd', models.DecimalField(blank=True, decimal_places=6, max_digits=12, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('suite', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='worlds', to='eval.evalsuite')),
            ],
            options={
                'verbose_name': 'Eval world',
                'verbose_name_plural': 'Eval worlds',
                'ordering': ['suite', 'version'],
                'indexes': [models.Index(fields=['suite', '-version'], name='eval_evalwo_suite_i_ac9c60_idx')],
            },
        ),
        migrations.AddConstraint(
            model_name='evalworld',
            constraint=models.UniqueConstraint(fields=('suite', 'version'), name='unique_world_version_per_suite'),
        ),
        migrations.AddField(
            model_name='evalcase',
            name='world_version',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='evalrun',
            name='world_version',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='evalresult',
            name='env_changes',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
