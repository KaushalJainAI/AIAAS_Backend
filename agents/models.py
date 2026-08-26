from django.db import models
from django.conf import settings
from django.utils.text import slugify
import uuid

from llm.providers import provider_choices


class SubAgent(models.Model):
    """
    A specialised agent: a prompt, a model, and the capabilities it is allowed.

    A row is a *configuration*, and the runtime is the only thing that turns
    one into a run.

    Every row is the same kind of thing. There is deliberately no `kind` column
    and no "orchestrator" variant — an agent that fans out to other agents is
    one holding the `subAgents` grant, so composition goes through the same
    grant check as every other capability rather than through a second code
    path. See `agents/agent/runtime.py::GRANT_TOOLS`.
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
        related_name='subagents',
    )

    # ── Identity ──
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, blank=True)
    description = models.TextField(
        blank=True,
        help_text='What this agent is for — shown when picking one to delegate to',
    )
    prompt = models.TextField(
        blank=True,
        default='',
        help_text='The specialisation: what this agent is, how it should work, '
                  'and what a good result looks like. Becomes its system prompt.',
    )

    # ── Model ──
    LLM_PROVIDER_CHOICES = provider_choices()

    llm_provider = models.CharField(
        max_length=30, choices=LLM_PROVIDER_CHOICES, default='openrouter',
    )
    llm_model = models.CharField(max_length=100, blank=True, default='')
    llm_credential = models.ForeignKey(
        'credentials.Credential',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='subagents_using_llm',
    )

    # ── Capability ──
    # Each column has one job, because the permissions screen shown at install
    # renders from these same rows the runtime enforces. A single opaque config
    # blob would let the screen and the enforcement drift apart, which is the
    # one failure this design cannot tolerate.
    tool_grants = models.JSONField(
        default=dict, blank=True,
        help_text='Which capabilities this agent may use, e.g. {"webSearch": true}',
    )
    requirements = models.JSONField(
        default=list, blank=True,
        help_text='Portable requirements for sharing — what kind of connection '
                  'or KB it needs, never the row ids of whoever authored it',
    )
    guardrails = models.JSONField(
        default=dict, blank=True,
        help_text='autonomy, spend cap, max delegation depth, egress policy',
    )
    agent_context = models.JSONField(
        default=dict, blank=True,
        help_text='What it is given: connector ids, knowledge base ids, skill ids',
    )
    sandbox = models.JSONField(
        default=dict, blank=True,
        help_text='Execution envelope: file access, workdir, venv, cpu, memory',
    )

    # ── Result shape ──
    # These two are why a *configured* agent can replace a hardcoded tool.
    # `deep_research` is 55 lines that fan out across queries and hand back a
    # fixed JSON contract the UI renders. An agent that can only return prose,
    # one query at a time, can never stand in for it however good its prompt
    # is — so the shape of the answer and the shape of the work are config,
    # not orchestrator-only code.
    output_schema = models.JSONField(
        default=dict, blank=True,
        help_text='Contract the result must satisfy. Empty means prose.',
    )
    fanout = models.JSONField(
        default=dict, blank=True,
        help_text='How it parallelises, e.g. {"parallel": 4, "mode": "collect"}. '
                  'Empty means a single sequential turn.',
    )

    runtime_settings = models.JSONField(
        default=dict, blank=True,
        help_text='Turn-level knobs: temperature, recursiveContext, compaction, '
                  'indexing, and the builder’s invocation mode.',
    )

    # ── Invocation ──
    allow_unattended = models.BooleanField(
        default=False,
        help_text='May run with nobody watching (schedule, webhook, event). Off '
                  'by default: a trigger is a way for something other than the '
                  'user to spend their model credits.',
    )

    # ── Presentation & state ──
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    is_template = models.BooleanField(default=False)
    tags = models.JSONField(default=list, blank=True)
    icon = models.CharField(max_length=50, blank=True)
    color = models.CharField(max_length=7, default='#6366f1')

    # Counters are denormalised for listing; `_with_stats` still computes the
    # observed numbers from ExecutionLog, because a stored counter drifts.
    execution_count = models.IntegerField(default=0)
    last_executed_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Sub-agent'
        verbose_name_plural = 'Sub-agents'
        ordering = ['-updated_at']
        unique_together = ['user', 'name']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['user', '-updated_at']),
            models.Index(fields=['user', '-updated_at', '-id']),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return self.status == 'active'

    @property
    def delegates(self) -> bool:
        """Whether this agent may invoke other agents."""
        return bool((self.tool_grants or {}).get('subAgents'))


class Trigger(models.Model):
    """Something other than the user asking an agent to run.

    A real table rather than a JSON blob on the agent, because the schedule
    sweep queries `next_due_at` across every user on a timer — that has to be
    an index, not a full scan with JSON parsing per row.

    A trigger answers one question: should this agent run now. It knows nothing
    about graphs or nodes, and polling triggers with cursors (email UIDs, RSS
    GUIDs) are deliberately out of scope — those are connector-side and a much
    larger thing than an invocation.
    """

    MODE_CHOICES = [
        ('schedule', 'Schedule'),
        ('webhook', 'Webhook'),
        ('event', 'Event'),
    ]

    #: What to do when a trigger fires while its previous run is still going.
    OVERLAP_CHOICES = [
        ('skip', 'Skip this firing'),
        ('queue', 'Run after the current one'),
        ('cancel', 'Cancel the running one'),
    ]

    subagent = models.ForeignKey(
        'SubAgent', on_delete=models.CASCADE, related_name='triggers',
    )
    mode = models.CharField(max_length=12, choices=MODE_CHOICES, db_index=True)
    config = models.JSONField(
        default=dict, blank=True,
        help_text='Mode-specific: {"cron": "0 9 * * 1"} or {"event": "run.completed"}',
    )
    goal = models.TextField(
        blank=True, default='',
        help_text='What the agent is asked to do when this fires.',
    )

    #: Path component for `mode='webhook'`. The endpoint is unauthenticated by
    #: construction — there is no session on an inbound hook — so this string
    #: is the only thing standing between the open internet and a run that
    #: spends the owner's model credits. Generated, never chosen.
    secret = models.CharField(max_length=64, unique=True, db_index=True, blank=True)

    enabled = models.BooleanField(default=True)
    overlap = models.CharField(
        max_length=8, choices=OVERLAP_CHOICES, default='skip',
    )

    last_fired_at = models.DateTimeField(null=True, blank=True)
    #: Indexed: the sweep's whole job is "which of these are due now".
    next_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    consecutive_failures = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Trigger'
        verbose_name_plural = 'Triggers'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['enabled', 'mode', 'next_due_at']),
            models.Index(fields=['subagent', 'mode']),
        ]

    def __str__(self):
        return f'{self.mode} trigger for {self.subagent_id}'

    def save(self, *args, **kwargs):
        # Every row needs a distinct secret: the column is unique, and schedule
        # triggers would otherwise collide on ''. Only webhook mode *surfaces*
        # it (via the serializer); the others just carry an opaque id.
        if not self.secret:
            self.secret = uuid.uuid4().hex + uuid.uuid4().hex[:16]
        super().save(*args, **kwargs)

    @property
    def cron(self) -> str:
        return (self.config or {}).get('cron', '')


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
    subagent = models.ForeignKey(
        'SubAgent',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='conversation_messages',
        help_text='Sub-agent this conversation is about, if any'
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
