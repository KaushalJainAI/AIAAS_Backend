"""Promote `input_data['thread_id']` to an indexed column, and backfill it.

The backfill is what makes the column safe to *read from* on the next request:
without it every run that predates this migration would look like a run with no
thread, and `_find_paused_log` would answer None for a paused run whose
checkpoint is sitting right there — an approval that silently resumes nothing.

Only rows that could still be looked up are worth carrying: a `thread_id` is
read to resume a paused run, to close a HITL request, and to resolve a parent
step. `paused` rows are the ones where being wrong is unrecoverable, so they
are backfilled first and unconditionally; the rest follow in batches because
this table is the largest in the schema on a busy account.

Reversible on purpose, and the reverse is a no-op beyond dropping the column:
`input_data` is still written, so nothing is lost by going back.
"""
from django.db import migrations, models


#: Batched rather than one `UPDATE ... json_extract(...)`: the expression is
#: spelled differently on SQLite and PostgreSQL, and this migration has to run
#: on both. Small enough not to hold a write lock on SQLite for long.
BATCH = 500


def backfill(apps, schema_editor):
    ExecutionLog = apps.get_model('logs', 'ExecutionLog')

    # Paused first: these are the rows a person is waiting on.
    for status_filter in ({'status': 'paused'}, {}):
        queryset = (
            ExecutionLog.objects
            .filter(thread_id='', **status_filter)
            .exclude(input_data={})
            .only('id', 'input_data', 'thread_id')
            .order_by('id')
        )
        pending = []
        for row in queryset.iterator(chunk_size=BATCH):
            thread = (row.input_data or {}).get('thread_id')
            if not isinstance(thread, str) or not thread:
                continue
            row.thread_id = thread[:200]
            pending.append(row)
            if len(pending) >= BATCH:
                ExecutionLog.objects.bulk_update(pending, ['thread_id'])
                pending.clear()
        if pending:
            ExecutionLog.objects.bulk_update(pending, ['thread_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('logs', '0017_add_cost_breakdown'),
    ]

    operations = [
        migrations.AddField(
            model_name='executionlog',
            name='thread_id',
            field=models.CharField(
                blank=True, db_index=True, default='', max_length=200,
                help_text='Checkpointer thread key; indexed copy of input_data.thread_id',
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
