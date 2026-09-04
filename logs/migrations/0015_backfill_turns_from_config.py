"""
Give historical runs the turns they always had, but never recorded.

Before `AgentTurn` existed, the turn a tool call belonged to lived in
`AgentStep.config['iteration']`, and the model's reasoning for it lived in
`config['thought']` — truncated to 150 characters and copied onto every call in
the turn. `graph_projection.project_run` reconstructed the loop from those two
keys at read time.

Without this migration every run recorded before today would lose its grouping
the moment the projection starts reading the `turn` FK: the canvas would draw a
straight line of tool calls, which is the one shape an agent loop is not.

Rows written before `iteration` was recorded at all fall back to one turn per
step — the same fallback the projection used, so nothing renders differently
than it did yesterday.

Reasoning recovered here is marked `reasoning_truncated=True`: it *was* truncated,
by the old write path, and a 150-character thought must not be mistaken for a
short one the model actually produced.
"""
from __future__ import annotations

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

#: Rows per bulk_create batch. Backfills run on boxes with 1.9 GB of RAM.
BATCH = 500


def forwards(apps, schema_editor):
    ExecutionLog = apps.get_model('logs', 'ExecutionLog')
    AgentTurn = apps.get_model('logs', 'AgentTurn')
    AgentStep = apps.get_model('logs', 'AgentStep')

    execution_ids = list(
        AgentStep.objects.filter(turn__isnull=True)
        .values_list('execution_id', flat=True)
        .distinct()
    )
    if not execution_ids:
        return

    turns_made = 0
    steps_linked = 0

    for execution_id in execution_ids:
        steps = list(
            AgentStep.objects.filter(execution_id=execution_id, turn__isnull=True)
            .order_by('order', 'id')
        )
        if not steps:
            continue

        # Group by the old iteration key, preserving first-seen order — exactly
        # what `project_run` did with the same data.
        grouped: dict[int, list] = {}
        for position, step in enumerate(steps):
            config = step.config if isinstance(step.config, dict) else {}
            iteration = config.get('iteration')
            if not isinstance(iteration, int):
                iteration = position + 1
            grouped.setdefault(iteration, []).append(step)

        for index in sorted(grouped):
            batch = grouped[index]
            config = batch[0].config if isinstance(batch[0].config, dict) else {}
            thought = str(config.get('thought') or '')
            turn = AgentTurn.objects.create(
                execution_id=execution_id,
                index=index,
                reasoning=thought,
                # It really was truncated — by the old writer, at 150 chars.
                reasoning_truncated=bool(thought),
                decision='tools',
            )
            turns_made += 1
            for step in batch:
                step.turn = turn
            AgentStep.objects.bulk_update(batch, ['turn'], batch_size=BATCH)
            steps_linked += len(batch)

    # Every run that has turns now has a decision on its last one that reflects
    # how it actually ended, rather than the 'tools' default.
    for execution in ExecutionLog.objects.filter(
        turns__isnull=False, status__in=('completed', 'failed', 'paused')
    ).distinct().iterator():
        last = AgentTurn.objects.filter(execution=execution).order_by('-index').first()
        if last is None:
            continue
        last.decision = {
            'completed': 'answer', 'failed': 'error', 'paused': 'paused',
        }[execution.status]
        last.save(update_fields=['decision'])

    logger.info(
        '[0015] Backfilled %s turn(s) across %s run(s), linking %s step(s).',
        turns_made, len(execution_ids), steps_linked,
    )


def backwards(apps, schema_editor):
    """Unlink the steps and drop the turns. `config` is untouched by this
    migration, so reversing loses nothing that was not already there."""
    AgentStep = apps.get_model('logs', 'AgentStep')
    AgentTurn = apps.get_model('logs', 'AgentTurn')
    AgentStep.objects.filter(turn__isnull=False).update(turn=None)
    AgentTurn.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('logs', '0014_add_turns_and_revisions'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
