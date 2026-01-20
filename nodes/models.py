from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator


class CustomNode(models.Model):
    """
    User-created custom nodes that extend the platform's functionality.
    Contains Python code that is validated before execution.
    """
    NODE_CATEGORY_CHOICES = [
        ('trigger', 'Trigger'),
        ('action', 'Action'),
        ('transform', 'Transform'),
        ('conditional', 'Conditional'),
        ('integration', 'Integration'),
        ('ai', 'AI/LLM'),
        ('utility', 'Utility'),
    ]
    
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending', 'Pending Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='custom_nodes'
    )
    
    # Node Identity
    name = models.CharField(
        max_length=100,
        help_text='Display name for the node'
    )
    node_type = models.CharField(
        max_length=100,
        unique=True,
        help_text='Unique identifier for the node type (e.g., custom_trello)'
    )
    category = models.CharField(
        max_length=20,
        choices=NODE_CATEGORY_CHOICES,
        default='action'
    )
    description = models.TextField(
        blank=True,
        help_text='Description of what this node does'
    )
    icon = models.CharField(
        max_length=50,
        blank=True,
        help_text='Icon class or emoji for the node'
    )
    color = models.CharField(
        max_length=7,
        default='#6366f1',
        help_text='Hex color for the node in the editor'
    )
    
    # Node Code
    code = models.TextField(
        help_text='Python code implementing the node handler'
    )
    fields_schema = models.JSONField(
        default=list,
        help_text='JSON schema defining node configuration fields'
    )
    input_schema = models.JSONField(
        default=dict,
        blank=True,
        help_text='Expected input data schema'
    )
    output_schema = models.JSONField(
        default=dict,
        blank=True,
        help_text='Output data schema'
    )
    
    # Validation & Status
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='draft'
    )
    is_validated = models.BooleanField(
        default=False,
        help_text='Whether the code has passed validation'
    )
    validation_errors = models.JSONField(
        default=list,
        blank=True,
        help_text='List of validation errors if any'
    )
    
    # Sharing
    is_public = models.BooleanField(
        default=False,
        help_text='Whether this node is available to other users'
    )
    
    # Metadata
    version = models.CharField(
        max_length=20,
        default='1.0.0'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Custom Node'
        verbose_name_plural = 'Custom Nodes'
        ordering = ['-created_at']
        unique_together = ['user', 'name']
        indexes = [
            models.Index(fields=['node_type']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['category', 'is_public']),
            models.Index(fields=['status', 'is_validated']),
        ]

    def __str__(self):
        return f"{self.name} ({self.node_type})"

    @property
    def is_ready(self):
        """Check if node is ready for use"""
        return self.status == 'approved' and self.is_validated
