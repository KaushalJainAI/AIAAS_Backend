"""Trigram indexes behind `find_files` (docs/VFS_HARDENING_PLAN.md §3).

Django spells `icontains` on Postgres as `UPPER(col::text) LIKE UPPER(%s)`, so
the indexes are on `UPPER(...)` or the planner cannot use them. Postgres only,
and **never fatal**: `pg_trgm` may be unavailable or the role may lack the
privilege to create it, and a search index is not worth a failed deploy — so
each step runs in its own savepoint and a failure is logged and skipped.
SQLite gets nothing; its search stays a scan of the user's own rows.
"""
import logging

from django.db import migrations, transaction

logger = logging.getLogger(__name__)

_INDEXES = (
    ('inf_doc_name_trgm', 'UPPER(name)'),
    ('inf_doc_text_trgm', 'UPPER(content_text)'),
)


def forwards(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'postgresql':
        return
    try:
        with transaction.atomic(using=connection.alias):
            with connection.cursor() as cursor:
                cursor.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    except Exception as exc:  # noqa: BLE001 — optional by design
        logger.warning('pg_trgm unavailable (%s); find_files stays unindexed.', exc)
        return
    for name, expr in _INDEXES:
        try:
            with transaction.atomic(using=connection.alias):
                with connection.cursor() as cursor:
                    cursor.execute(
                        f'CREATE INDEX IF NOT EXISTS {name} ON inference_document '
                        f'USING gin ({expr} gin_trgm_ops)'
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning('Could not create %s (%s); skipped.', name, exc)


def backwards(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        for name, _ in _INDEXES:
            cursor.execute(f'DROP INDEX IF EXISTS {name}')


class Migration(migrations.Migration):

    dependencies = [
        ('inference', '0023_recent_files_app_sessions'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
