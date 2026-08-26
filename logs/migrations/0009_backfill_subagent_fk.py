"""
Point every historical row at the `SubAgent` that replaced its `Workflow`.

`0016_migrate_agents_to_subagent` carried the primary keys across, so this is
`subagent_id = workflow_id` rather than a name match — restricted to ids that
actually exist on the new table, which is exactly the set of rows that were
`kind='agent'`. Logs belonging to a node graph keep a null `subagent`: there is
no agent they could point at, and inventing one would be worse than a gap.
"""
from __future__ import annotations

import logging

from django.db import migrations
from django.db.models import F

logger = logging.getLogger(__name__)

#: (app label, model name) for every table that carried a `workflow` FK.
TARGETS = (
    ('logs', 'ExecutionLog'),
    ('logs', 'AuditEntry'),
    ('logs', 'OrchestratorThought'),
    ('orchestrator', 'ConversationMessage'),
)


def forwards(apps, schema_editor):
    SubAgent = apps.get_model('orchestrator', 'SubAgent')

    migrated_ids = set(SubAgent.objects.values_list('id', flat=True))
    if not migrated_ids:
        return

    for app_label, model_name in TARGETS:
        model = apps.get_model(app_label, model_name)
        updated = model.objects.filter(
            workflow_id__in=migrated_ids, subagent_id__isnull=True,
        ).update(subagent_id=F('workflow_id'))
        if updated:
            logger.info('[0009] Repointed %s %s row(s).', updated, model_name)


def backwards(apps, schema_editor):
    for app_label, model_name in TARGETS:
        apps.get_model(app_label, model_name).objects.update(subagent_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ('logs', '0008_add_subagent_fk'),
        ('orchestrator', '0017_add_subagent_fk'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
