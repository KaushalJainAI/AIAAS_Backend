"""
The per-user document tree, part 2 of 2: `Document.folder` as a real FK.

Separate from 0013 so the drop of the dead CharField and the add of the FK are
not the same SQLite table rebuild (see 0013's docstring), and so "the dead
column is gone" reviews independently of "the tree exists".

Nothing to backfill. `folder_id IS NULL` *is* the user's root, so every row that
already exists is correctly placed the moment the column appears — which is what
lets a fresh `migrate` yield a working install with no data migration and no
lazy root creation.

`SET_NULL` is a deliberate choice, not a default. A cascade would delete
Documents through the ORM collector, which never runs
`tasks.remove_document_from_kb` — orphaning FAISS vectors and IndexedTerm
postings for rows that no longer exist. Losing a folder must not lose the files
in it; they surface at the root, still indexed.

`PROTECT` was tried first, to have the database enforce the purge sweep's
documents-before-folders ordering. It cannot be used here: Django evaluates
`on_delete` during the collector's *collection* pass, with no exemption for
protected rows that are themselves being collected, so deleting a user — where
Folder and Document both cascade off `user` — raised ProtectedError and account
deletion became impossible. The ordering now lives in
`recycle.run_recycle_sweep` and is pinned by its tests.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inference', '0013_folder'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='folder',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='documents', to='inference.folder',
                help_text=(
                    "NULL is the user's root. SET_NULL, emphatically not CASCADE: a cas"
                    'cade would take Documents out through the ORM collector, which fir'
                    'es post_delete but never runs tasks.remove_document_from_kb — leav'
                    'ing FAISS vectors and IndexedTerm postings for rows that no longer'
                    ' exist, which RAG then answers with a dangling id. Losing a folder'
                    ' must therefore never lose the files in it; they surface at the ro'
                    'ot instead, still indexed and still findable. PROTECT was tried fi'
                    "rst, to make the database enforce the purge sweep's documents-befo"
                    're-folders ordering. It cannot be used: Django evaluates on_delete'
                    " during the collector's *collection* pass, with no exemption for p"
                    'rotected rows that are themselves being collected, so deleting a u'
                    'ser — where Folder and Document both cascade off `user` — raised P'
                    'rotectedError and account deletion was impossible. The ordering is'
                    ' enforced by `recycle.run_recycle_sweep` and pinned by its tests i'
                    'nstead.'
                )),
        ),
        migrations.AddIndex(
            model_name='document',
            index=models.Index(fields=['user', 'folder', '-created_at'],
                               name='inference_d_user_id_1e682c_idx'),
        ),
    ]
