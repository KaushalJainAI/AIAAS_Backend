"""
Re-derive `file_type` from the name for rows the old vocabulary mistyped.

`normalize_file_type` used to file old Office binaries as their new-format
cousins (`.doc` as `docx`, `.xls` as `xlsx`, `.ppt` as `pptx`), which the
new-format code then failed to open everywhere it touched them. New rows are
typed `doc_legacy` / `xls_legacy` / `ppt_legacy` at write time; this one-off
repairs rows written before that, plus any `other` row whose extension now
says otherwise:

    python manage.py retype_documents
    python manage.py retype_documents --dry-run
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Re-derive file_type from the name for mistyped rows.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything.')

    def handle(self, *args, **options):
        from inference.models import Document
        from inference.utils import normalize_file_type

        # Only rows whose stored type disagrees with today's vocabulary *and*
        # whose stored type is one the re-mapping replaces. A hand-corrected
        # row keeps its type: the extension is a hint, not an order.
        candidates = Document.all_objects.filter(
            file_type__in=('other', 'docx', 'xlsx', 'pptx'))
        changed = 0
        for doc in candidates.iterator():
            correct = normalize_file_type(doc.name)
            if correct != doc.file_type and (
                    correct.endswith('_legacy')
                    or (doc.file_type == 'other' and correct != 'other')):
                if options['dry_run']:
                    self.stdout.write(f'Would retype {doc.id} {doc.name!r}: '
                                      f'{doc.file_type} -> {correct}')
                else:
                    Document.all_objects.filter(id=doc.id).update(file_type=correct)
                changed += 1
        where = 'would retype' if options['dry_run'] else 'Retyped'
        self.stdout.write(f'{where} {changed} document(s).')
