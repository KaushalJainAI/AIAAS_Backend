import logging
from celery import shared_task
from django.utils import timezone
from asgiref.sync import async_to_sync

from .models import Document, KnowledgeBase
from .engine import get_hnsw_kb, get_kb_manager
from .utils import extract_text_from_file

logger = logging.getLogger(__name__)


@shared_task(bind=True, time_limit=600, soft_time_limit=540)
def process_document_task(self, document_id, kb_id=None):
    return DocumentIndexingService.process_document(document_id, kb_id=kb_id)


@shared_task(bind=True, time_limit=600, soft_time_limit=540)
def share_document_task(self, document_id, user_id):
    return DocumentIndexingService.share_document(document_id, user_id)


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
        Index a document into its assigned KnowledgeBase (or the user's Default KB).
        Runs synchronously (from Thread or Celery); uses async_to_sync for HNSW calls.
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

            hnsw = get_hnsw_kb(kb_model.id, kb_model.s3_index_key or f'indices/kb_{kb_model.id}')

            async def _index():
                await hnsw.initialize()
                chunks = await hnsw.add_document(
                    doc.id,
                    doc.content_text or '',
                    {'name': doc.name, 'user_id': doc.user.id, 'kb_id': kb_model.id},
                )
                return chunks

            chunks = async_to_sync(_index)()

            doc.chunk_count = len(chunks)
            doc.status = 'indexed'
            doc.indexed_at = timezone.now()
            doc.save(update_fields=['chunk_count', 'status', 'indexed_at'])

            KnowledgeBase.objects.filter(id=kb_model.id).update(
                doc_count=Document.objects.filter(knowledge_base_id=kb_model.id).count(),
                vector_count=hnsw.ntotal,
                index_size_bytes=hnsw.index_size_bytes,
            )

            logger.info(f"Indexed doc {document_id} into KB {kb_model.id} ({len(chunks)} chunks)")
            return f"Indexed {len(chunks)} chunks into KB {kb_model.id}"

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
    def share_document(document_id, user_id):
        """Add document to the platform-wide shared KB (KB id=-1 by convention)."""
        try:
            doc = Document.objects.get(id=document_id)
            logger.info(f"Sharing document {document_id} to platform KB")
            from .engine import get_platform_knowledge_base
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
