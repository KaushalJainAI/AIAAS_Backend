"""
Move `kind='agent'` rows onto `SubAgent`, and put the DAG rows somewhere safe.

`Workflow` is dropped later in this sequence. Agents are copied field by field
rather than by renaming the table, because most of `Workflow` is not coming
with them: `nodes`, `edges`, `viewport`, `workflow_settings`,
`supervision_level` and the clone/template columns all belonged to the node
graph, and carrying them forward is what would keep the old product alive
inside the new one.

`kind='workflow'` rows are *not* migrated — there is no runtime left that could
execute one. They are written to a JSON file first anyway, because "we have no
way to run these" and "you may throw these away" are different statements and
only the user gets to make the second one.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from django.db import migrations
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Copied straight across — same name, same meaning on both models.
DIRECT_FIELDS = (
    'name', 'slug', 'description',
    'llm_provider', 'llm_model', 'llm_credential_id',
    'tool_grants', 'requirements', 'guardrails', 'agent_context', 'sandbox',
    'status', 'is_template', 'tags', 'icon', 'color',
    'execution_count', 'last_executed_at',
)


def _dump_dag_workflows(Workflow, base_dir: Path) -> int:
    """Write every remaining node graph to disk before it stops existing."""
    rows = list(
        Workflow.objects.filter(kind='workflow').values(
            'id', 'user_id', 'name', 'slug', 'description', 'context',
            'nodes', 'edges', 'viewport', 'workflow_settings',
            'supervision_level', 'status', 'tags', 'created_at', 'updated_at',
        )
    )
    if not rows:
        return 0

    stamp = timezone.now().strftime('%Y%m%d-%H%M%S')
    target = base_dir / f'dag_workflows_backup_{stamp}.json'
    try:
        target.write_text(
            json.dumps(rows, indent=2, default=str), encoding='utf-8',
        )
    except OSError:
        # A read-only filesystem must not turn "you kept a backup" into "the
        # deploy failed". The rows are still in the table at this point; the
        # drop is a later migration, so there is another chance to take one.
        logger.exception(
            '[0016] Could not write the DAG workflow backup. %s row(s) remain '
            'in orchestrator_workflow and will be dropped by a later '
            'migration — export them before running it.', len(rows),
        )
        return 0

    logger.warning(
        '[0016] Wrote %s DAG workflow(s) to %s. They are not migrated: there '
        'is no runtime left that can execute a node graph.', len(rows), target,
    )
    return len(rows)


def forwards(apps, schema_editor):
    Workflow = apps.get_model('orchestrator', 'Workflow')
    SubAgent = apps.get_model('orchestrator', 'SubAgent')

    # Primary keys are carried across deliberately. `SubAgent` is a fresh
    # table, so its ids are free, and reusing them turns the FK backfill on
    # ExecutionLog / AuditEntry / OrchestratorThought / ConversationMessage
    # into `subagent_id = workflow_id` — no mapping table, and no chance of two
    # agents with the same name colliding during the match.

    from django.conf import settings

    _dump_dag_workflows(Workflow, Path(settings.BASE_DIR))

    agents = Workflow.objects.filter(kind='agent')
    created: list[tuple[int, object, object]] = []

    for agent in agents.iterator():
        fields = {name: getattr(agent, name) for name in DIRECT_FIELDS}
        row = SubAgent.objects.create(
            id=agent.id,
            user_id=agent.user_id,
            # The agent's brief lived in `context`, a column named for what the
            # retired supervisor read rather than for what an agent is.
            prompt=agent.context or '',
            # Off for every migrated row. Unattended invocation is an explicit
            # decision, and no existing row could have made it: the trigger
            # runtime was deleted, so nothing has run unattended in any case.
            allow_unattended=False,
            output_schema={},
            fanout={},
            **fields,
        )
        created.append((row.pk, agent.created_at, agent.updated_at))

    # `auto_now_add` / `auto_now` overwrote these on create. Restored with
    # `update()`, which does not re-trigger them — a listing ordered by
    # `-updated_at` would otherwise show every agent as touched today.
    for pk, created_at, updated_at in created:
        SubAgent.objects.filter(pk=pk).update(
            created_at=created_at, updated_at=updated_at,
        )

    if not created:
        return

    # Explicit ids do not advance a Postgres sequence, so the next ordinary
    # insert would collide with a migrated row. SQLite derives the next rowid
    # from MAX(id) and needs nothing.
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(
            "SELECT setval(pg_get_serial_sequence('orchestrator_subagent', 'id'), "
            "COALESCE((SELECT MAX(id) FROM orchestrator_subagent), 1))"
        )

    logger.info('[0016] Migrated %s agent(s) to SubAgent.', len(created))


def backwards(apps, schema_editor):
    """Empty the new table. The `Workflow` rows were never removed."""
    apps.get_model('orchestrator', 'SubAgent').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('orchestrator', '0015_add_subagent'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
