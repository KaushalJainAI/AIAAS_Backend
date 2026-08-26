"""
Merge `extraction` into `inference` (2026-08-18).

Creates the extraction models under their natural `inference_*` table names and
cleans up the old app's residue:

- drops the old `extraction_extraction*` tables (guarded: a fresh install never
  had them),
- clears the old app's `django_migrations` rows,
- clears the old app's permissions and orphaned `django_content_type` rows.

The old tables only ever held seed data (`seed_improve.py`), so dropping is the
default; a deployment with real extraction data must use the data-move branch
instead (see docs/EXTRACTION_MERGE.md §3.2).
"""
from django.db import migrations, models
import django.db.models.deletion
import django.conf


def drop_old_extraction_tables(apps, schema_editor):
    """Drop the retired app's tables and its registry rows, if any exist."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        for table in ('extraction_extractionschema', 'extraction_extractedrow'):
            if table in tables:
                cursor.execute(f'DROP TABLE "{table}"')
        cursor.execute("DELETE FROM django_migrations WHERE app = 'extraction'")
        # Permissions point at the orphaned content types; raw SQL bypasses the
        # ORM's CASCADE, so delete them first or the FK check at the end of the
        # migration fails. The app is gone, so its permissions are gone too.
        cursor.execute(
            "DELETE FROM auth_permission WHERE content_type_id IN "
            "(SELECT id FROM django_content_type WHERE app_label = 'extraction')"
        )
        cursor.execute("DELETE FROM django_content_type WHERE app_label = 'extraction'")


class Migration(migrations.Migration):

    dependencies = [
        ('inference', '0008_document_keyset_pagination_indexes'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExtractionSchema',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('fields', models.JSONField(default=list, help_text='The columns to fill')),
                ('source_kind', models.CharField(choices=[('upload', 'Manual upload'), ('gmail', 'Gmail'), ('gdrive', 'Google Drive')], default='upload', max_length=20)),
                ('source_ref', models.CharField(blank=True, help_text='Label, folder or query the documents come from', max_length=300)),
                ('confidence_threshold', models.FloatField(default=0.8, help_text='Below this, a row is held for review rather than accepted')),
                ('llm_model', models.CharField(blank=True, help_text='Model used to extract rows. Blank resolves to the default vision model.', max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='extraction_schemas', to=django.conf.settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-updated_at'],
                'indexes': [models.Index(fields=['user', '-updated_at'], name='inference_extraction_user_id_idx')],
                'unique_together': {('user', 'name')},
            },
        ),
        migrations.CreateModel(
            name='ExtractedRow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('document_name', models.CharField(max_length=300)),
                ('data', models.JSONField(default=dict, help_text='Field name -> extracted value')),
                ('field_confidence', models.JSONField(blank=True, default=dict, help_text='Field name -> confidence, so review can point at the cell')),
                ('confidence', models.FloatField(default=0.0, help_text='Lowest field confidence in the row')),
                ('status', models.CharField(choices=[('accepted', 'Accepted'), ('needs_review', 'Needs review'), ('reviewed', 'Reviewed'), ('rejected', 'Rejected')], db_index=True, default='accepted', max_length=20)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('document', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='extracted_rows', to='inference.document')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_extractions', to=django.conf.settings.AUTH_USER_MODEL)),
                ('schema', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rows', to='inference.extractionschema')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['schema', 'status'], name='inference_extracted_row_schema_idx'), models.Index(fields=['schema', '-created_at'], name='inference_extracted_row_schema_created_idx')],
            },
        ),
        migrations.RunPython(drop_old_extraction_tables, migrations.RunPython.noop),
    ]