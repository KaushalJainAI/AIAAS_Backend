from django.db import models
from django.conf import settings

# We will move WorkflowTemplate here.
# NOTE: Since WorkflowTemplate was defined in orchestrator.models previously,
# we need to be careful with migrations. But assuming we are refactoring.

class WorkflowTemplate(models.Model):
    """
    Reusable templates for workflows.
    Separates 'library blueprints' from user-owned workflows.
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending_test', 'Pending Test'),
        ('verified', 'Verified'),
        ('production', 'Production'),
        ('deprecated', 'Deprecated'),
    ]
    
    # Basic Info
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=100, blank=True)
    tags = models.JSONField(default=list, blank=True)
    
    # Definition
    nodes = models.JSONField(default=list)
    edges = models.JSONField(default=list)
    workflow_settings = models.JSONField(default=dict, blank=True)
    
    # Lifecycle
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='draft'
    )
    
    # Metrics & Quality
    success_rate = models.FloatField(default=0.0)
    average_duration_ms = models.IntegerField(null=True, blank=True)
    usage_count = models.IntegerField(default=0)
    
    # Modification Control
    modifiable_fields = models.JSONField(default=list, blank=True)
    is_cloneable = models.BooleanField(default=True)
    
    # AI Search
    embedding = models.BinaryField(null=True, blank=True)
    
    # Lineage
    parent_template = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='versions'
    )
    
    # Source Workflow (for auto-publish)
    source_workflow_id = models.IntegerField(null=True, blank=True, help_text="ID of original workflow if auto-published")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-usage_count', '-success_rate']

    def __str__(self):
        return f"{self.name} ({self.status})"
