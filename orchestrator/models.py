from django.db import models
from django.conf import settings
from django.utils.text import slugify
import uuid


class Workflow(models.Model):
    """
    User's workflow definitions containing nodes and edges.
    The core entity that users create and execute.
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('archived', 'Archived'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='workflows'
    )
    
    # Identity
    name = models.CharField(
        max_length=200,
        help_text='Workflow name'
    )
    slug = models.SlugField(
        max_length=200,
        blank=True
    )
    description = models.TextField(
        blank=True,
        help_text='Description of what this workflow does'
    )
    
    # Workflow Definition
    nodes = models.JSONField(
        default=list,
        help_text='Array of node definitions'
    )
    edges = models.JSONField(
        default=list,
        help_text='Array of edge connections'
    )
    viewport = models.JSONField(
        default=dict,
        blank=True,
        help_text='Canvas viewport state (zoom, pan position)'
    )
    
    # Settings
    workflow_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text='Workflow-level settings (timeout, retry config, etc.)'
    )
    
    # Status
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='draft'
    )
    is_template = models.BooleanField(
        default=False,
        help_text='Whether this workflow is a template'
    )
    
    # Metadata
    tags = models.JSONField(
        default=list,
        blank=True,
        help_text='Tags for organizing workflows'
    )
    icon = models.CharField(
        max_length=50,
        blank=True
    )
    color = models.CharField(
        max_length=7,
        default='#6366f1'
    )
    
    # Execution Stats
    execution_count = models.IntegerField(
        default=0,
        help_text='Total number of executions'
    )
    last_executed_at = models.DateTimeField(
        blank=True,
        null=True
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Workflow'
        verbose_name_plural = 'Workflows'
        ordering = ['-updated_at']
        unique_together = ['user', 'name']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['user', '-updated_at']),
            models.Index(fields=['status', '-updated_at']),
            models.Index(fields=['is_template', 'status']),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def node_count(self):
        """Number of nodes in the workflow"""
        return len(self.nodes) if isinstance(self.nodes, list) else 0

    @property
    def is_active(self):
        """Check if workflow is currently active"""
        return self.status == 'active'

    # Subworkflow & Template Support
    is_subworkflow_enabled = models.BooleanField(
        default=True,
        help_text='Allow this workflow to be used as a subworkflow'
    )
    max_nesting_depth = models.IntegerField(
        default=3,
        help_text='Maximum allowed nesting depth to prevent infinite recursion'
    )
    parent_template = models.ForeignKey(
        'templates.WorkflowTemplate',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_workflows',
        help_text='Template this workflow was cloned from'
    )
    modifiable_fields = models.JSONField(
        default=list,
        blank=True,
        help_text='List of fields that can be safely modified during cloning'
    )
    is_cloneable = models.BooleanField(
        default=True,
        help_text='Whether this workflow can be cloned by the orchestrator'
    )
    
    # Performance Metrics
    total_executions = models.IntegerField(
        default=0,
        help_text='Total number of times this workflow has been executed'
    )
    successful_executions = models.IntegerField(
        default=0,
        help_text='Total number of successful executions'
    )
    average_duration_ms = models.IntegerField(
        null=True,
        blank=True,
        help_text='Average execution duration in milliseconds'
    )

    @property
    def success_rate(self):
        """Calculated success rate percentage"""
        if self.total_executions == 0:
            return 0.0
        return (self.successful_executions / self.total_executions) * 100


class WorkflowVersion(models.Model):
    """
    Version history for workflows.
    Stores snapshots of workflow state for undo/restore.
    """
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name='versions'
    )
    
    # Version Info
    version_number = models.IntegerField(
        help_text='Sequential version number'
    )
    label = models.CharField(
        max_length=100,
        blank=True,
        help_text='Optional label for this version'
    )
    
    # Snapshot
    nodes = models.JSONField(default=list)
    edges = models.JSONField(default=list)
    workflow_settings = models.JSONField(default=dict, blank=True)
    
    # Metadata
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='workflow_versions'
    )
    change_summary = models.TextField(
        blank=True,
        help_text='Summary of changes in this version'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Workflow Version'
        verbose_name_plural = 'Workflow Versions'
        ordering = ['-version_number']
        unique_together = ['workflow', 'version_number']
        indexes = [
            models.Index(fields=['workflow', '-version_number']),
        ]

    def __str__(self):
        label = f" - {self.label}" if self.label else ""
        return f"{self.workflow.name} v{self.version_number}{label}"


class HITLRequest(models.Model):
    """
    Human-in-the-Loop requests for approval, clarification, or error recovery.
    Blocks execution until user responds.
    """
    REQUEST_TYPE_CHOICES = [
        ('approval', 'Approval Required'),
        ('clarification', 'Clarification Needed'),
        ('error_recovery', 'Error Recovery'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('answered', 'Answered'),
        ('timeout', 'Timeout'),
        ('cancelled', 'Cancelled'),
    ]
    
    request_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True
    )
    
    # Context
    execution = models.ForeignKey(
        'logs.ExecutionLog',
        on_delete=models.CASCADE,
        related_name='hitl_requests'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='hitl_requests'
    )
    node_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='ID of the node requesting human input'
    )
    
    # Request Details
    request_type = models.CharField(
        max_length=20,
        choices=REQUEST_TYPE_CHOICES
    )
    title = models.CharField(
        max_length=200,
        help_text='Short title for the request'
    )
    message = models.TextField(
        help_text='Detailed message explaining what is needed'
    )
    options = models.JSONField(
        default=list,
        blank=True,
        help_text='Available options/choices for the user'
    )
    context_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Additional context data'
    )
    
    # Response
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    response = models.JSONField(
        default=dict,
        blank=True,
        help_text='User response data'
    )
    responded_at = models.DateTimeField(
        blank=True,
        null=True
    )
    
    # Timeout
    timeout_seconds = models.IntegerField(
        default=300,
        help_text='Timeout in seconds (0 = no timeout)'
    )
    auto_action = models.CharField(
        max_length=50,
        blank=True,
        help_text='Action to take on timeout (e.g., approve, reject, skip)'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'HITL Request'
        verbose_name_plural = 'HITL Requests'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['request_id']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['execution', 'status']),
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        return f"{self.request_type}: {self.title}"

    @property
    def is_pending(self):
        """Check if request is still waiting for response"""
        return self.status == 'pending'


class ConversationMessage(models.Model):
    """
    AI chat conversation history.
    Stores messages between user and AI assistant.
    """
    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('system', 'System'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversation_messages'
    )
    
    # Conversation Context
    conversation_id = models.UUIDField(
        default=uuid.uuid4,
        help_text='Groups messages into conversations'
    )
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='conversation_messages',
        help_text='Workflow context if applicable'
    )
    
    # Message
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES
    )
    content = models.TextField(
        help_text='Message content'
    )
    
    # Metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Additional metadata (tokens used, model, etc.)'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Conversation Message'
        verbose_name_plural = 'Conversation Messages'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['user', 'conversation_id']),
            models.Index(fields=['conversation_id', 'created_at']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"{self.role}: {preview}"





class WorkflowTestResult(models.Model):
    """
    Results of async workflow testing.
    Validates correctness and performance before production use.
    """
    STATUS_CHOICES = [
        ('passed', 'Passed'),
        ('failed', 'Failed'),
        ('timeout', 'Timeout'),
        ('error', 'Error'),
    ]
    
    test_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    
    # Target
    workflow_template = models.ForeignKey(
        'templates.WorkflowTemplate',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='test_results'
    )
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='test_results'
    )
    
    # Result
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    
    # Data
    test_input = models.JSONField(default=dict, help_text='Synthetic input used during test')
    test_output = models.JSONField(default=dict, help_text='Actual output received')
    expected_output_schema = models.JSONField(default=dict, blank=True)
    
    # Performance & Error
    execution_time_ms = models.IntegerField(null=True)
    error_message = models.TextField(blank=True)
    error_node_id = models.CharField(max_length=100, blank=True)
    
    # Validation Flags
    schema_valid = models.BooleanField(default=False)
    timeout_within_limit = models.BooleanField(default=False)
    no_exceptions = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)


class WorkflowCloneHistory(models.Model):
    """
    Tracks the lineage of workflow creation and modification.
    Useful for debugging and understanding AI generation patterns.
    """
    parent_workflow = models.ForeignKey(
        Workflow,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='clones'
    )
    child_workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name='clone_source'
    )
    
    # Change Tracking
    modifications_applied = models.JSONField(
        default=dict,
        help_text='Diff of what changed during cloning'
    )
    
    # Context
    cloned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True
    )
    reason = models.TextField(blank=True, help_text='Why this clone was created')
    
    created_at = models.DateTimeField(auto_now_add=True)

