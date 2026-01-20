from django.db import models
from django.conf import settings
import uuid


class StreamEvent(models.Model):
    """
    Persisted streaming events for replay and debugging.
    Stores SSE/WebSocket events that were broadcast during execution.
    """
    EVENT_TYPE_CHOICES = [
        ('node_start', 'Node Start'),
        ('node_complete', 'Node Complete'),
        ('node_error', 'Node Error'),
        ('workflow_start', 'Workflow Start'),
        ('workflow_complete', 'Workflow Complete'),
        ('workflow_error', 'Workflow Error'),
        ('hitl_request', 'HITL Request'),
        ('hitl_response', 'HITL Response'),
        ('progress', 'Progress Update'),
        ('thinking', 'AI Thinking'),
        ('planning', 'AI Planning'),
        ('log', 'Log Message'),
    ]
    
    event_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True
    )
    
    # Context
    execution = models.ForeignKey(
        'logs.ExecutionLog',
        on_delete=models.CASCADE,
        related_name='stream_events'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='stream_events'
    )
    
    # Event Details
    event_type = models.CharField(
        max_length=30,
        choices=EVENT_TYPE_CHOICES
    )
    node_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='Node ID if applicable'
    )
    
    # Payload
    data = models.JSONField(
        default=dict,
        help_text='Event data payload'
    )
    
    # Ordering
    sequence = models.IntegerField(
        default=0,
        help_text='Sequence number for ordering events'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Stream Event'
        verbose_name_plural = 'Stream Events'
        ordering = ['execution', 'sequence']
        indexes = [
            models.Index(fields=['event_id']),
            models.Index(fields=['execution', 'sequence']),
            models.Index(fields=['execution', 'event_type']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f"{self.event_type} ({self.event_id})"
