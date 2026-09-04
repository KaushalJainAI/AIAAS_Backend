"""
`NodeExecutionLog` -> `AgentStep`: a run's steps are tool calls, not graph nodes.

The columns were named for a DAG that no longer exists. `node_id` never held a
node id — `agents/agent/stream.py` writes the provider's tool-call id into it —
and `node_type` held the tool name. `node_name` was always set to the same value
as `node_type`, and `error_stack` / `retry_count` were never written by anything.

Renames only. The `config` column survives this migration on purpose: 0015 reads
its `{iteration, thought}` blob to backfill `AgentTurn` rows for runs recorded
before the turn table existed, and 0016 drops it afterwards.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('logs', '0012_drop_dag_era_logging'),
    ]

    operations = [
        # Indexes first: their generated names embed the old table and column
        # names, so renaming underneath them would leave three indexes whose
        # names describe columns that no longer exist.
        migrations.RemoveIndex(
            model_name='nodeexecutionlog',
            name='logs_nodeex_executi_f98217_idx',
        ),
        migrations.RemoveIndex(
            model_name='nodeexecutionlog',
            name='logs_nodeex_executi_a8d207_idx',
        ),
        migrations.RemoveIndex(
            model_name='nodeexecutionlog',
            name='logs_nodeex_node_ty_548577_idx',
        ),

        # Renames the table to the new default, `logs_agentstep`, because the
        # model sets no explicit `db_table`. An AlterModelTable here would
        # only pin the name Django already derives.
        migrations.RenameModel(
            old_name='NodeExecutionLog',
            new_name='AgentStep',
        ),
        migrations.RenameField(
            model_name='agentstep', old_name='node_id', new_name='call_id',
        ),
        migrations.RenameField(
            model_name='agentstep', old_name='node_type', new_name='tool',
        ),
        migrations.RenameField(
            model_name='agentstep', old_name='execution_order', new_name='order',
        ),
        migrations.RenameField(
            model_name='agentstep', old_name='input_data', new_name='args',
        ),
        migrations.RenameField(
            model_name='agentstep', old_name='output_data', new_name='result',
        ),

        migrations.RemoveField(model_name='agentstep', name='node_name'),
        migrations.RemoveField(model_name='agentstep', name='error_stack'),
        migrations.RemoveField(model_name='agentstep', name='retry_count'),

        migrations.AlterModelOptions(
            name='agentstep',
            options={
                'ordering': ['order'],
                'verbose_name': 'Agent step',
                'verbose_name_plural': 'Agent steps',
            },
        ),
    ]
