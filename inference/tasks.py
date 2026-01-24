import logging
from celery import shared_task
from django.utils import timezone
from .models import Document
from .engine import get_user_knowledge_base
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)

@shared_task(bind=True, time_limit=600, soft_time_limit=540)
def process_document_task(self, document_id):
    """
    Celery task wrapper for document processing.
    """
    return process_document(document_id)


from .utils import extract_text_from_file

def process_document(document_id):
    """
    Process and index a document.
    Can be run synchronously, via Thread, or via Celery.
    """
    try:
        doc = Document.objects.get(id=document_id)
        
        # Update status to processing
        doc.status = 'processing'
        doc.save()
        
        logger.info(f"Starting processing for document {document_id}: {doc.name}")
        
        # Extract text if not already present
        if not doc.content_text and doc.file:
            try:
                logger.info(f"Extracting text from file: {doc.file.path}")
                doc.content_text = extract_text_from_file(doc.file.path, doc.file_type)
                doc.save(update_fields=['content_text'])
            except Exception as e:
                logger.error(f"Failed to extract text: {e}")
                # We continue, maybe there is some partial text or we just index empty
        
        # Initialize Knowledge Base
        user_kb = get_user_knowledge_base(doc.user.id)
        
        # N.B. get_user_knowledge_base and its methods are likely async, 
        # so we need to run them synchronously here since Celery tasks are typically sync.
        # However, checking inference/views.py, they were awaited.
        # We'll use async_to_sync wrapper.
        
        async def _process():
            await user_kb.initialize()
            chunks = await user_kb.add_document(doc.id, doc.content_text, {
                'name': doc.name,
                'user_id': doc.user.id,
            })
            return chunks

        chunks = async_to_sync(_process)()
        
        # Update document success state
        doc.chunk_count = len(chunks)
        doc.status = 'indexed'
        doc.indexed_at = timezone.now()
        doc.save()
        
        logger.info(f"Successfully processed document {document_id}. Chunks: {len(chunks)}")
        return f"Indexed {len(chunks)} chunks"

    except Document.DoesNotExist:
        logger.error(f"Document {document_id} not found")
        return "Document not found"
        
    except Exception as e:
        logger.error(f"Error processing document {document_id}: {str(e)}", exc_info=True)
        try:
            doc = Document.objects.get(id=document_id)
            doc.status = 'failed'
            doc.error_message = str(e)
            doc.save()
        except:
            pass
        # Don't re-raise if running in thread, just log. 
        # But for Celery retry it might be useful. 
        # For simple threading, we catch everything here so the thread dies quietly after updating DB.
        return f"Failed: {str(e)}"
