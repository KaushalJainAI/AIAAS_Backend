"""
Run an extraction schema's LLM extraction over a set of documents, synchronously.

Mirror of `notifications/reminders.py` and the trigger sweep: local dev has no
Redis and a Celery-only path silently never fires. Usage:

    python manage.py run_extraction <schema_id> [--document <id> ...]
"""
from django.core.management.base import BaseCommand, CommandError

from inference.extraction import run_extraction
from inference.models import ExtractionSchema


class Command(BaseCommand):
    help = 'Run an extraction schema over documents (sync; no Redis needed).'

    def add_arguments(self, parser):
        parser.add_argument('schema_id', type=int)
        parser.add_argument('--document', type=int, action='append', dest='document_ids',
                            help='Document id to extract (repeatable). Default: all the user\'s documents.')

    def handle(self, *args, **options):
        schema_id = options['schema_id']
        try:
            schema = ExtractionSchema.objects.select_related('user').get(id=schema_id)
        except ExtractionSchema.DoesNotExist:
            raise CommandError(f'No schema with id {schema_id}')

        document_ids = options.get('document_ids')
        if not document_ids:
            from inference.models import Document
            document_ids = list(
                Document.objects.filter(user=schema.user).values_list('id', flat=True)[:100]
            )
        if not document_ids:
            raise CommandError('No documents to extract (pass --document, or add documents first).')

        self.stdout.write(f"Extracting {len(document_ids)} document(s) against '{schema.name}' "
                          f"using {schema.effective_model} ...")
        stats = run_extraction(document_ids, schema.id, schema.user_id)
        self.stdout.write(self.style.SUCCESS(f"Done: {stats}"))