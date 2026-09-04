import logging
from celery import shared_task
from django.utils import timezone
from asgiref.sync import async_to_sync

from .models import Document, KnowledgeBase
from .engine import get_hnsw_kb, get_platform_knowledge_base, sync_kb_stats
from .utils import extract_text_from_file

logger = logging.getLogger(__name__)


@shared_task(bind=True, time_limit=600, soft_time_limit=540)
def process_document_task(self, document_id, kb_id=None):
    return DocumentIndexingService.process_document(document_id, kb_id=kb_id)


@shared_task(bind=True, time_limit=600, soft_time_limit=540)
def share_document_task(self, document_id, user_id):
    return DocumentIndexingService.share_document(document_id, user_id)


@shared_task(name='inference.sweep_recycle_bin', ignore_result=True)
def sweep_recycle_bin():
    """Beat entry point for the recycle-bin purge.

    A dispatch wrapper only — the work is in `inference/recycle.py`, shared with
    `manage.py purge_recycle_bin` so a broker-less deployment can still run it.
    """
    from .recycle import run_recycle_sweep
    return run_recycle_sweep()


@shared_task(bind=True, time_limit=1800, soft_time_limit=1620)
def extract_documents_task(self, document_ids, schema_id, user_id):
    """Celery wrapper for the LLM extraction engine (async dispatch path)."""
    from .extraction import run_extraction
    return run_extraction(document_ids, schema_id, user_id)


def refresh_kb_stats(kb_model: KnowledgeBase) -> None:
    """Recount one KB's stats from live state. Sync; safe from any thread.

    Vector columns are only written when a vector index is actually loaded —
    a fulltext or raw KB has none, and an evicted instance would report zero
    vectors for a KB that has plenty.
    """
    hnsw = None
    if kb_model.uses_embeddings:
        candidate = get_hnsw_kb(
            kb_model.id, kb_model.s3_index_key or f'indices/kb_{kb_model.id}'
        )
        if candidate.is_loaded:
            hnsw = candidate
    sync_kb_stats(kb_model.id, hnsw)


async def remove_document_from_kb(kb_model: KnowledgeBase, doc_id: int) -> bool:
    """
    Drop one document from every mechanism its KB maintains. The one removal
    door — a KB's backends each clean their own state, so adding a backend
    never means finding the next delete path.
    """
    from .backends import get_ingest_backends

    removed = False
    for backend in get_ingest_backends(kb_model):
        try:
            if await backend.remove_document(doc_id):
                removed = True
        except Exception:
            logger.error(
                "Backend %s failed to remove doc %s from KB %s",
                backend.backend_name, doc_id, kb_model.id, exc_info=True,
            )
    return removed


class DocumentIndexingService:

    @staticmethod
    def _get_or_create_default_kb(user) -> KnowledgeBase:
        kb, _ = KnowledgeBase.objects.get_or_create(
            user=user,
            is_default=True,
            defaults={'name': 'Default', 'description': 'Auto-created default knowledge base'},
        )
        return kb

    @staticmethod
    def process_document(document_id, kb_id=None):
        """
        Ingest a document into its assigned KnowledgeBase (or the user's
        Default KB). The KB's `backend` decides what that means — semantic
        embedding, keyword indexing, raw storage, or both at once.

        Runs synchronously (from Thread or Celery); uses async_to_sync for
        engine calls.
        """
        try:
            doc = Document.objects.select_related('user', 'knowledge_base').get(id=document_id)
            doc.status = 'processing'
            doc.save(update_fields=['status'])

            logger.info(f"Processing document {document_id}: {doc.name}")

            if not doc.content_text and doc.file:
                try:
                    doc.content_text = extract_text_from_file(doc.file.path, doc.file_type)
                    doc.save(update_fields=['content_text'])
                except Exception as e:
                    logger.error(f"Text extraction failed for doc {document_id}: {e}")

            if kb_id is not None:
                kb_model = KnowledgeBase.objects.get(id=kb_id, user=doc.user)
            elif doc.knowledge_base_id:
                kb_model = doc.knowledge_base
            else:
                kb_model = DocumentIndexingService._get_or_create_default_kb(doc.user)
                doc.knowledge_base = kb_model
                doc.save(update_fields=['knowledge_base'])

            from .backends import get_ingest_backends

            async def _ingest():
                results = []
                for backend in get_ingest_backends(kb_model):
                    results.append(await backend.ingest(doc))
                return results

            results = async_to_sync(_ingest)()

            # Every backend agrees on the terminal state; 'stored' (raw) and
            # 'indexed' are the two outcomes. A hybrid run reports indexed.
            statuses = {r.status for r in results}
            final_status = 'stored' if statuses == {'stored'} else 'indexed'
            chunk_count = max((r.chunk_count for r in results), default=0)

            doc.chunk_count = chunk_count
            doc.status = final_status
            doc.indexed_at = timezone.now()
            doc.save(update_fields=['chunk_count', 'status', 'indexed_at'])

            DocumentIndexingService._sync_kb_stats(kb_model, results)

            detail = '; '.join(r.detail for r in results if r.detail)
            logger.info(
                f"Ingested doc {document_id} into KB {kb_model.id} "
                f"[{kb_model.backend}] ({detail or 'no content'})"
            )
            return f"Processed via {kb_model.backend}: {detail}"

        except Document.DoesNotExist:
            logger.error(f"Document {document_id} not found")
            return "Document not found"
        except Exception as e:
            logger.error(f"Error processing document {document_id}: {e}", exc_info=True)
            try:
                doc = Document.objects.get(id=document_id)
                doc.status = 'failed'
                doc.error_message = str(e)
                doc.save(update_fields=['status', 'error_message'])
            except Exception:
                pass
            return f"Failed: {e}"

    @staticmethod
    def _sync_kb_stats(kb_model: KnowledgeBase, results) -> None:
        """
        Reflect what ingestion produced back onto the KB row.

        Vector-derived stats (vector_count / index_size_bytes) only move when
        a vector-capable backend ran — a fulltext KB's index lives in the DB,
        not on disk. Doc count moves for every backend.

        A hybrid ingest composes one IngestResult out of two, and must carry
        the vector half's `extras` up with it — building a fresh result and
        dropping them meant a hybrid KB reported 0 vectors and 0 bytes for
        ever, however much it held.
        """
        vector_result = next((r for r in results if 'ntotal' in r.extras), None)
        updates = {
            'doc_count': Document.objects.filter(knowledge_base_id=kb_model.id).count(),
        }
        if vector_result is not None:
            updates['vector_count'] = vector_result.extras['ntotal']
            updates['index_size_bytes'] = vector_result.extras['index_size_bytes']
        KnowledgeBase.objects.filter(id=kb_model.id).update(**updates)

    @staticmethod
    def share_document(document_id, user_id):
        """Add document to the platform-wide shared KB (KB id=-1 by convention).

        The platform KB stays vector-only by convention regardless of the
        source KB's backend: it exists for cross-user semantic discovery."""
        try:
            doc = Document.objects.get(id=document_id)
            logger.info(f"Sharing document {document_id} to platform KB")
            platform_kb = get_platform_knowledge_base()

            async def _share():
                await platform_kb.initialize()
                if await platform_kb.has_document(doc.id):
                    return "Skipped (duplicate)"
                await platform_kb.add_document(doc.id, doc.content_text or '', {
                    'name': doc.name,
                    'user_id': user_id,
                    'shared': True,
                    'sharing_mode': doc.sharing_mode,
                })
                return "Added"

            result = async_to_sync(_share)()
            logger.info(f"Platform KB: doc {document_id} → {result}")
            return result

        except Exception as e:
            logger.error(f"Error sharing document {document_id}: {e}", exc_info=True)
            return f"Failed: {e}"


# Module-level aliases for backward compatibility
process_document = DocumentIndexingService.process_document
share_document = DocumentIndexingService.share_document
