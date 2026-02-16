from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator

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
    
    CATEGORY_CHOICES = [
        ('marketing', 'Marketing'),
        ('devops', 'DevOps'),
        ('data', 'Data Pipeline'),
        ('support', 'Customer Support'),
        ('ai_ml', 'AI / ML'),
        ('sales', 'Sales'),
        ('hr', 'HR & Recruiting'),
        ('finance', 'Finance'),
        ('other', 'Other'),
    ]
    
    # Basic Info
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='other')
    tags = models.JSONField(default=list, blank=True)
    
    # Author Info
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='published_templates'
    )
    author_name = models.CharField(max_length=150, blank=True)
    
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
    
    # Community & Ratings
    average_rating = models.FloatField(default=0.0)
    rating_count = models.IntegerField(default=0)
    is_featured = models.BooleanField(default=False)
    fork_count = models.IntegerField(default=0)
    
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
        ordering = ['-is_featured', '-usage_count', '-average_rating']

    def __str__(self):
        return f"{self.name} ({self.status})"


class WorkflowRating(models.Model):
    """
    User star ratings and reviews for templates.
    """
    template = models.ForeignKey(WorkflowTemplate, on_delete=models.CASCADE, related_name='ratings')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    stars = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    review = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('template', 'user')


class WorkflowBookmark(models.Model):
    """
    User bookmarks for quick access to templates.
    """
    template = models.ForeignKey(WorkflowTemplate, on_delete=models.CASCADE, related_name='bookmarks')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('template', 'user')


class TemplateComment(models.Model):
    """
    Threaded community comments/discussions for templates.
    """
    template = models.ForeignKey(WorkflowTemplate, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text = models.TextField()
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='replies'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
