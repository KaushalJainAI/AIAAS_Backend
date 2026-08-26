"""
The per-user document tree, part 1 of 2: the Folder model, the recycle-bin
columns, and the removal of the dead `Document.folder` CharField.

Hand-written rather than auto-generated. `makemigrations` offers the CharField →
ForeignKey change as a single `AlterField`, which is wrong in a way that only
shows up on real data: it asks the database to reinterpret the old `folder`
column as `folder_id`, and every existing row holds `''`. SQLite rebuilds the
table and copies the empty strings into an integer column; PostgreSQL refuses
the cast outright. The column is dead (nothing has ever written it), so the
honest operation is a drop and a separate add — which is also why the add lives
in 0014: dropping and adding the same attribute name inside one SQLite table
rebuild makes `sqlmigrate` unreadable and the rollback ambiguous.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import inference.utils


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inference', '0012_alter_document_is_shared'),
    ]

    operations = [
        migrations.CreateModel(
            name='Folder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('path', models.CharField(
                    blank=True, db_index=True, max_length=1000,
                    help_text='Materialised ancestry as slash-delimited *ids*, '
                              'self-inclusive: "/12/45/78/". Ids, not names — a name '
                              'path would make rename O(descendants) and would put a '
                              'user-controlled path string back in the database. '
                              'Written by save(); never accepted from a client.')),
                ('depth', models.PositiveSmallIntegerField(default=0)),
                ('deleted_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('trashed_directly', models.BooleanField(
                    default=False,
                    help_text='True only on the node the user actually deleted. '
                              'Descendants carry deleted_at but not this, so the trash '
                              'view lists one entry per delete instead of one per row '
                              'in the subtree.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('parent', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='children', to='inference.folder',
                    help_text="NULL is the user's root. There is deliberately no root "
                              "row: NULL is unforgeable — it can never be another "
                              "user's folder — so the root is one fewer id on the "
                              'attack surface, and a root row would be a second '
                              'spelling of "in root" that the chat upload paths would '
                              'never write.')),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='folders', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Folder',
                'verbose_name_plural': 'Folders',
                'ordering': ['name'],
            },
        ),
        # The dead column. Declared in 0001_initial as a "virtual folder path",
        # never read or written by application code in its whole life.
        migrations.RemoveField(model_name='document', name='folder'),
        migrations.AddField(
            model_name='document',
            name='deleted_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='document',
            name='trashed_directly',
            field=models.BooleanField(
                default=False,
                help_text='True only on the row the user actually deleted.'),
        ),
        migrations.AlterField(
            model_name='document',
            name='file',
            field=models.FileField(upload_to=inference.utils.user_document_path),
        ),
        migrations.AddIndex(
            model_name='folder',
            index=models.Index(fields=['user', 'parent'],
                               name='inference_f_user_id_1a3817_idx'),
        ),
        migrations.AddIndex(
            model_name='folder',
            index=models.Index(fields=['user', 'path'],
                               name='inference_f_user_id_e240e0_idx'),
        ),
        migrations.AddIndex(
            model_name='folder',
            index=models.Index(fields=['user', 'deleted_at'],
                               name='inference_f_user_id_fe13ef_idx'),
        ),
        migrations.AddConstraint(
            model_name='folder',
            constraint=models.UniqueConstraint(
                condition=models.Q(('deleted_at__isnull', True), ('parent__isnull', False)),
                fields=('user', 'parent', 'name'),
                name='unique_folder_name_per_parent'),
        ),
        # SQL treats NULLs as distinct, so the constraint above does not reach
        # top-level folders at all. Two constraints, one rule.
        migrations.AddConstraint(
            model_name='folder',
            constraint=models.UniqueConstraint(
                condition=models.Q(('deleted_at__isnull', True), ('parent__isnull', True)),
                fields=('user', 'name'),
                name='unique_root_folder_name_per_user'),
        ),
        migrations.AddIndex(
            model_name='document',
            index=models.Index(fields=['user', 'deleted_at'],
                               name='inference_d_user_id_7e9cef_idx'),
        ),
    ]
