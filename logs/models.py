from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
import uuid


class ExecutionLog(models.Model):
    """
    Records workflow execution history.
    Tracks overall execution status, timing, and results.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('paused', 'Paused'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
        ('timeout', 'Timeout'),
    ]
    
    TRIGGER_CHOICES = [
        ('manual', 'Manual'),
        ('schedule', 'Schedule'),
        ('webhook', 'Webhook'),
        ('api', 'API'),
    ]
    
    execution_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text='Unique identifier for this execution'
    )
    workflow = models.ForeignKey(
        'orchestrator.Workflow',
        on_delete=models.CASCADE,
        related_name='executions'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='execution_logs'
    )
    
    # Execution Details
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    trigger_type = models.CharField(
        max_length=20,
        choices=TRIGGER_CHOICES,
        default='manual'
    )
    
    # Subworkflow Support
    parent_execution = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='child_executions',
        help_text='Parent execution if this is a subworkflow'
    )
    nesting_depth = models.IntegerField(
        default=0,
        help_text='Depth in the execution hierarchy (0 = root)'
    )
    is_subworkflow_execution = models.BooleanField(
        default=False,
        help_text='Flag to identify subworkflow executions'
    )
    workflow_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text='Snapshot of workflow definition at execution time'
    )
    timeout_budget_ms = models.IntegerField(
        null=True,
        blank=True,
        help_text='Allocated timeout budget in milliseconds'
    )
    loop_iterations = models.IntegerField(
        default=0,
        help_text='Count of loops within this execution'
    )
    
    # Timing
    started_at = models.DateTimeField(
        blank=True,
        null=True
    )
    completed_at = models.DateTimeField(
        blank=True,
        null=True
    )
    duration_ms = models.IntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(0)],
        help_text='Execution duration in milliseconds'
    )
    
    # Input/Output
    input_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Input data passed to the workflow'
    )
    output_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Final output from the workflow'
    )
    
    # Error Handling
    error_message = models.TextField(
        blank=True,
        help_text='Error message if execution failed'
    )
    error_node_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='ID of the node that caused the error'
    )
    
    # Resource Usage
    nodes_executed = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)]
    )
    tokens_used = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)]
    )
    credits_used = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)]
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Execution Log'
        verbose_name_plural = 'Execution Logs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['execution_id']),
            models.Index(fields=['workflow', '-created_at']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['trigger_type', '-created_at']),
        ]

    def __str__(self):
        return f"Execution {self.execution_id} ({self.status})"

    @property
    def is_complete(self):
        """Check if execution has finished (success or failure)"""
        return self.status in ['completed', 'failed', 'cancelled', 'timeout']


class NodeExecutionLog(models.Model):
    """
    Records individual node execution within a workflow.
    Provides detailed logging for debugging and monitoring.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ]
    
    execution = models.ForeignKey(
        ExecutionLog,
        on_delete=models.CASCADE,
        related_name='node_logs'
    )
    
    # Node Identification
    node_id = models.CharField(
        max_length=100,
        help_text='ID of the node in the workflow'
    )
    node_type = models.CharField(
        max_length=100,
        help_text='Type of node (e.g., http_request, code, openai)'
    )
    node_name = models.CharField(
        max_length=200,
        blank=True,
        help_text='Display name of the node'
    )
    
    # Subworkflow Support
    is_subworkflow_node = models.BooleanField(
        default=False,
        help_text='Whether this node executed a subworkflow'
    )
    child_execution = models.ForeignKey(
        ExecutionLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='parent_node_log',
        help_text='Link to the subworkflow execution'
    )
    input_mapping_applied = models.JSONField(
        default=dict,
        blank=True,
        help_text='Input variable mapping applied'
    )
    output_mapping_applied = models.JSONField(
        default=dict,
        blank=True,
        help_text='Output variable mapping applied'
    )
    
    # Execution Details
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    execution_order = models.IntegerField(
        default=0,
        help_text='Order in which this node was executed'
    )
    
    # Timing
    started_at = models.DateTimeField(
        blank=True,
        null=True
    )
    completed_at = models.DateTimeField(
        blank=True,
        null=True
    )
    duration_ms = models.IntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(0)]
    )
    
    # Data
    input_data = models.JSONField(
        default=dict,
        blank=True
    )
    output_data = models.JSONField(
        default=dict,
        blank=True
    )
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text='Node configuration at execution time'
    )
    
    # Error
    error_message = models.TextField(blank=True)
    error_stack = models.TextField(
        blank=True,
        help_text='Stack trace if available'
    )
    retry_count = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)]
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Node Execution Log'
        verbose_name_plural = 'Node Execution Logs'
        ordering = ['execution_order']
        indexes = [
            models.Index(fields=['execution', 'node_id']),
            models.Index(fields=['execution', 'status']),
            models.Index(fields=['node_type', 'status']),
        ]

    def __str__(self):
        return f"{self.node_name or self.node_id} ({self.status})"


class AuditEntry(models.Model):
    """
    Audit trail for sensitive actions and HITL decisions.
    Used for compliance, debugging, and accountability.
    """
    ACTION_TYPE_CHOICES = [
        ('approval', 'Approval Request'),
        ('clarification', 'Clarification Request'),
        ('error_recovery', 'Error Recovery'),
        ('credential_access', 'Credential Access'),
        ('workflow_modify', 'Workflow Modification'),
        ('admin_action', 'Admin Action'),
        ('api_access', 'API Access'),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='audit_entries'
    )
    
    # Context
    workflow = models.ForeignKey(
        'orchestrator.Workflow',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_entries'
    )
    execution = models.ForeignKey(
        ExecutionLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_entries'
    )
    node_id = models.CharField(
        max_length=100,
        blank=True
    )
    
    # Action Details
    action_type = models.CharField(
        max_length=30,
        choices=ACTION_TYPE_CHOICES
    )
    request_details = models.JSONField(
        default=dict,
        help_text='Details of what was requested/asked'
    )
    response = models.JSONField(
        default=dict,
        blank=True,
        help_text='User response or action taken'
    )
    
    # Timing
    response_time_ms = models.IntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(0)],
        help_text='Time taken to respond in milliseconds'
    )
    
    # Metadata
    ip_address = models.GenericIPAddressField(
        blank=True,
        null=True
    )
    user_agent = models.TextField(blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Audit Entry'
        verbose_name_plural = 'Audit Entries'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['workflow', '-created_at']),
            models.Index(fields=['action_type', '-created_at']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f"{self.action_type} by {self.user} at {self.created_at}"
