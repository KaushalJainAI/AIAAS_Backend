"""
Dead Letter Queue - Failed Task Management

Handles failed Celery tasks for retry and debugging.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from django.utils import timezone

logger = logging.getLogger(__name__)


class DeadLetterEntry:
    """A failed task entry in the dead letter queue."""
    
    def __init__(
        self,
        task_id: str,
        task_name: str,
        args: tuple,
        kwargs: dict,
        exception: str,
        traceback: str,
        retry_count: int = 0,
        created_at: datetime | None = None,
    ):
        self.id = str(uuid4())
        self.task_id = task_id
        self.task_name = task_name
        self.args = args
        self.kwargs = kwargs
        self.exception = exception
        self.traceback = traceback
        self.retry_count = retry_count
        self.created_at = created_at or datetime.utcnow()
        self.last_retry_at: datetime | None = None
        self.status = "pending"  # pending, retrying, resolved, expired
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "args": list(self.args),
            "kwargs": self.kwargs,
            "exception": self.exception,
            "traceback": self.traceback,
            "retry_count": self.retry_count,
            "created_at": self.created_at.isoformat(),
            "last_retry_at": self.last_retry_at.isoformat() if self.last_retry_at else None,
            "status": self.status,
        }


class DeadLetterQueue:
    """
    Dead Letter Queue for failed Celery tasks.
    
    Features:
    - Stores failed tasks for inspection
    - Supports manual retry
    - Automatic expiration
    - Task grouping by type
    
    Usage:
        dlq = DeadLetterQueue()
        
        # Add failed task
        dlq.add(task_id, task_name, args, kwargs, exception, traceback)
        
        # Get pending entries
        entries = dlq.get_pending()
        
        # Retry an entry
        await dlq.retry(entry_id)
    """
    
    def __init__(self, max_retries: int = 3, expiry_days: int = 7):
        self._entries: dict[str, DeadLetterEntry] = {}
        self.max_retries = max_retries
        self.expiry_days = expiry_days
    
    def add(
        self,
        task_id: str,
        task_name: str,
        args: tuple,
        kwargs: dict,
        exception: str,
        traceback: str = "",
    ) -> str:
        """
        Add a failed task to the queue.
        
        Returns the entry ID.
        """
        entry = DeadLetterEntry(
            task_id=task_id,
            task_name=task_name,
            args=args,
            kwargs=kwargs,
            exception=exception,
            traceback=traceback,
        )
        
        self._entries[entry.id] = entry
        
        logger.warning(
            f"Task added to DLQ: {task_name} ({task_id}), "
            f"error: {exception[:100]}"
        )
        
        # Also save to database for persistence
        self._save_to_db(entry)
        
        return entry.id
    
    def get(self, entry_id: str) -> DeadLetterEntry | None:
        """Get an entry by ID."""
        return self._entries.get(entry_id)
    
    def get_pending(self, limit: int = 50) -> list[DeadLetterEntry]:
        """Get all pending entries."""
        entries = [
            e for e in self._entries.values()
            if e.status == "pending"
        ]
        return sorted(entries, key=lambda e: e.created_at, reverse=True)[:limit]
    
    def get_by_task_name(self, task_name: str) -> list[DeadLetterEntry]:
        """Get entries for a specific task type."""
        return [
            e for e in self._entries.values()
            if e.task_name == task_name
        ]
    
    async def retry(self, entry_id: str) -> bool:
        """
        Retry a failed task.
        
        Returns True if retry was initiated.
        """
        entry = self._entries.get(entry_id)
        if not entry:
            return False
        
        if entry.retry_count >= self.max_retries:
            entry.status = "expired"
            logger.warning(f"DLQ entry {entry_id} exceeded max retries")
            return False
        
        entry.status = "retrying"
        entry.retry_count += 1
        entry.last_retry_at = datetime.utcnow()
        
        try:
            # Get the Celery task and retry
            from celery import current_app
            
            task = current_app.tasks.get(entry.task_name)
            if task:
                task.apply_async(
                    args=entry.args,
                    kwargs=entry.kwargs,
                    countdown=60,  # Retry after 1 minute
                )
                
                logger.info(f"Retrying DLQ entry {entry_id}: {entry.task_name}")
                return True
            else:
                logger.error(f"Task not found: {entry.task_name}")
                entry.status = "pending"
                return False
                
        except Exception as e:
            logger.exception(f"Failed to retry DLQ entry: {e}")
            entry.status = "pending"
            return False
    
    def resolve(self, entry_id: str) -> bool:
        """Mark an entry as resolved (manually handled)."""
        entry = self._entries.get(entry_id)
        if entry:
            entry.status = "resolved"
            return True
        return False
    
    def remove(self, entry_id: str) -> bool:
        """Remove an entry from the queue."""
        if entry_id in self._entries:
            del self._entries[entry_id]
            return True
        return False
    
    def cleanup_expired(self) -> int:
        """Remove expired entries."""
        cutoff = datetime.utcnow() - timedelta(days=self.expiry_days)
        
        expired = [
            entry_id for entry_id, entry in self._entries.items()
            if entry.created_at < cutoff or entry.status == "expired"
        ]
        
        for entry_id in expired:
            del self._entries[entry_id]
        
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired DLQ entries")
        
        return len(expired)
    
    def get_stats(self) -> dict:
        """Get queue statistics."""
        status_counts = {}
        task_counts = {}
        
        for entry in self._entries.values():
            status_counts[entry.status] = status_counts.get(entry.status, 0) + 1
            task_counts[entry.task_name] = task_counts.get(entry.task_name, 0) + 1
        
        return {
            "total": len(self._entries),
            "by_status": status_counts,
            "by_task": task_counts,
        }
    
    def _save_to_db(self, entry: DeadLetterEntry) -> None:
        """Save entry to database for persistence."""
        # Using Django's cache or a model would be better for production
        # This is a placeholder
        pass
    
    def load_from_db(self) -> None:
        """Load entries from database on startup."""
        # Placeholder for database loading
        pass


# Celery signal handler to capture failures
def setup_celery_failure_handler():
    """Set up Celery signal to capture task failures."""
    try:
        from celery.signals import task_failure
        
        @task_failure.connect
        def handle_task_failure(
            sender=None,
            task_id=None,
            exception=None,
            args=None,
            kwargs=None,
            traceback=None,
            einfo=None,
            **kw
        ):
            """Handle task failure by adding to DLQ."""
            dlq = get_dead_letter_queue()
            
            dlq.add(
                task_id=task_id or "unknown",
                task_name=sender.name if sender else "unknown",
                args=args or (),
                kwargs=kwargs or {},
                exception=str(exception),
                traceback=str(einfo) if einfo else "",
            )
        
        logger.info("Celery failure handler registered")
        
    except ImportError:
        logger.warning("Celery not installed, DLQ handler not registered")


# Global instance
_dlq: DeadLetterQueue | None = None


def get_dead_letter_queue() -> DeadLetterQueue:
    """Get global dead letter queue."""
    global _dlq
    if _dlq is None:
        _dlq = DeadLetterQueue()
    return _dlq
