"""
What an agent did, what it was thinking, and what it was configured as.

An agent run is a **loop of turns**, not a pipeline of nodes. Each turn the model
reasons, issues zero or more tool calls, gets every result back into the *same*
model, and reasons again. The four tables here follow that shape:

    SubAgentRevision ──┐
                       ├─ ExecutionLog ── AgentTurn ── AgentStep
    (the config)          (one run)      (one model    (one tool
                                          call)         call)

- `ExecutionLog` — one run: status, timing, spend, and the answer.
- `AgentTurn` — one model call: its full reasoning, what it decided, and which
  model actually served it.
- `AgentStep` — one tool call, hanging off the turn that issued it.
- `SubAgentRevision` — the configuration a run executed under.

**Why the turn is a row.** It used to be reconstructed at read time by grouping
`config['iteration']` out of a JSON blob on each step, and the reasoning attached
to a step was `thinking[-150:]` — the same 150-character slice copied onto every
call in the turn, with the full text discarded when the run closed. A turn that
has to be inferred cannot be queried, and reasoning that is truncated to a
tweet cannot be debugged.

**Why the config is a row.** `SubAgent` carried only `updated_at`, so "it started
behaving badly last Tuesday" had no answer. `ExecutionLog.revision` pins each run
to the exact configuration that produced it.

**Why delegation points at a step.** `ExecutionLog.parent_step` names the tool
call that asked for the run, not merely the run that contained it — so
`parent_step.execution` is the orchestrating run and `parent_step.turn.reasoning`
is what it was thinking when it delegated. A run-to-run link would have given the
first and lost the second.

History: `AuditEntry` and `OrchestratorThought` lived here until 2026-08-19, and
`AgentStep` was `NodeExecutionLog` until 2026-08-19. All three were DAG-era.
"""
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class SubAgentRevision(models.Model):
    """One saved version of an agent's configuration.

    The snapshot is the flat `AgentConfig` dict that
    `agents.views.agents.AgentSerializer.to_config` already produces for the
    builder. Reusing it rather than serialising the columns again means a
    revision is diffable and renderable with no second mapping that could drift
    from the one the UI reads.

    A revision is only written when something actually changed — see
    `logs/revisions.py::record`. A save that changed nothing must not appear in
    the timeline, or the timeline stops being a record of decisions.
    """

    SOURCE_CHOICES = [
        ('create', 'Created'),
        ('update', 'Updated'),
        # Minted lazily for an agent that predates revision tracking, so its
        # first run still has a configuration to point at.
        ('backfill', 'Backfilled'),
    ]

    subagent = models.ForeignKey(
        'orchestrator.SubAgent',
        on_delete=models.CASCADE,
        related_name='revisions',
    )
    #: Who made the change. Null for a backfill, which nobody performed.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='agent_revisions',
    )

    number = models.IntegerField(help_text='1-based, per agent')
    config = models.JSONField(
        default=dict, help_text='Full AgentConfig snapshot at this revision'
    )
    diff = models.JSONField(
        default=dict, blank=True,
        help_text="{field: {'from': …, 'to': …}} against the previous revision",
    )
    summary = models.CharField(
        max_length=300, blank=True,
        help_text='Human-scannable list of what changed, e.g. "model, autonomy"',
    )
    source = models.CharField(max_length=12, choices=SOURCE_CHOICES, default='update')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Sub-agent revision'
        verbose_name_plural = 'Sub-agent revisions'
        ordering = ['-number']
        unique_together = ['subagent', 'number']
        indexes = [
            models.Index(fields=['subagent', '-number']),
            models.Index(fields=['subagent', '-created_at']),
        ]

    def __str__(self):
        return f'{self.subagent_id} rev {self.number}'


class ExecutionLog(models.Model):
    """One agent run: status, timing, resource usage, and the final result."""

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

    #: Who started the run. `trigger_type` says how it was invoked; this says
    #: *what* invoked it, and they are not the same question — a chat-invoked
    #: run and a delegated worker both record `trigger_type='api'`, and telling
    #: them apart is the difference between "the user asked for this" and "an
    #: agent decided to spend the user's credits on it".
    CALLER_CHOICES = [
        ('api', 'Direct API'),
        ('chat', 'Chat agent'),
        ('orchestrator', 'Delegated by another agent'),
        ('trigger', 'Trigger'),
    ]

    execution_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text='Unique identifier for this execution',
    )
    # Null for runs whose agent has since been deleted, and for the historical
    # rows that belonged to a node graph — see migration 0009.
    subagent = models.ForeignKey(
        'orchestrator.SubAgent',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='executions',
    )
    #: The configuration this run actually executed under. Null only for runs
    #: that predate revision tracking. This is the field that makes "why did it
    #: behave differently this time" answerable.
    revision = models.ForeignKey(
        SubAgentRevision,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='executions',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='execution_logs',
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    trigger_type = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default='manual')
    caller = models.CharField(max_length=20, choices=CALLER_CHOICES, default='api')

    # ── Delegation ──
    #: The tool call that asked for this run. Points at the *step* rather than
    #: the parent run so that the orchestrator's reasoning is one hop away:
    #: `parent_step.turn.reasoning`. SET_NULL because a parent's steps may be
    #: pruned while the worker run stays interesting on its own.
    parent_step = models.ForeignKey(
        'AgentStep',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='delegated_runs',
    )
    #: The instruction the worker was given. Generated by the parent model at
    #: run time, so it exists nowhere else once the parent's transcript is gone.
    delegation_task = models.TextField(blank=True)
    delegation_index = models.IntegerField(
        default=0, help_text='Position within the fan-out that produced this run'
    )
    depth = models.IntegerField(
        default=0, help_text='Delegation depth; 0 is a run the user started'
    )

    # ── Timing ──
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    duration_ms = models.IntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(0)],
        help_text='Execution duration in milliseconds',
    )

    # ── Input/output ──
    input_data = models.JSONField(
        default=dict, blank=True, help_text='Input data passed to the run'
    )
    output_data = models.JSONField(
        default=dict, blank=True, help_text='Final output from the run'
    )

    # ── Errors ──
    error_message = models.TextField(blank=True, help_text='Error message if the run failed')
    error_node_id = models.CharField(
        max_length=100, blank=True, help_text='call_id of the step that caused the error'
    )

    # ── Resource usage ──
    nodes_executed = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    tokens_used = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    credits_used = models.IntegerField(default=0, validators=[MinValueValidator(0)])

    supervision_level = models.CharField(
        max_length=20, blank=True, help_text='Level of supervision used for this run'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Execution Log'
        verbose_name_plural = 'Execution Logs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['execution_id']),
            models.Index(fields=['subagent', '-created_at']),
            # The three-column variants back keyset pagination, which orders by
            # ('-created_at', '-id') so that rows sharing a timestamp still have
            # a total order to page through.
            models.Index(fields=['subagent', '-created_at', '-id']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['user', '-created_at', '-id']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['trigger_type', '-created_at']),
            models.Index(fields=['caller', '-created_at']),
            # "Show me every worker this run spawned" — the delegation tree.
            models.Index(fields=['parent_step', 'delegation_index']),
            models.Index(fields=['revision', '-created_at']),
        ]

    def __str__(self):
        return f"Execution {self.execution_id} ({self.status})"

    @property
    def is_complete(self):
        """Whether the run has finished, successfully or not."""
        return self.status in ('completed', 'failed', 'cancelled', 'timeout')

    @property
    def is_delegated(self) -> bool:
        return self.parent_step_id is not None


class AgentTurn(models.Model):
    """One pass of the model: what it thought, and what it decided to do next.

    This is the unit of agent decision-making. A run is a sequence of these, and
    every tool call belongs to exactly one — the calls sharing a turn were issued
    *together*, and their results all return to the next turn rather than to each
    other. That grouping is the whole difference between drawing an agent as the
    loop it is and drawing it as a pipeline it never was.
    """

    DECISION_CHOICES = [
        ('tools', 'Called tools'),
        ('answer', 'Answered'),
        ('paused', 'Paused for approval'),
        ('error', 'Failed'),
    ]

    execution = models.ForeignKey(
        ExecutionLog, on_delete=models.CASCADE, related_name='turns'
    )
    index = models.IntegerField(help_text='1-based, within the run')

    #: The model's own reasoning for this turn, in full — not the 150-character
    #: slice the old `config['thought']` carried. Bounded by
    #: `TURN_REASONING_CHAR_LIMIT`; `reasoning_truncated` says when it was cut,
    #: because a trimmed thought and a short one must not look alike.
    reasoning = models.TextField(blank=True)
    reasoning_truncated = models.BooleanField(default=False)
    #: The visible text this turn produced. Usually empty on a tool-calling
    #: turn and the answer on the last one.
    content = models.TextField(blank=True)
    content_truncated = models.BooleanField(default=False)

    decision = models.CharField(max_length=12, choices=DECISION_CHOICES, default='tools')

    #: Recorded per turn rather than per run: a run's model can resolve
    #: differently on a resume, and "which model produced this" is the first
    #: question when an answer is wrong.
    provider = models.CharField(max_length=30, blank=True)
    model_id = models.CharField(max_length=200, blank=True)

    tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    duration_ms = models.IntegerField(
        blank=True, null=True, validators=[MinValueValidator(0)]
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Agent turn'
        verbose_name_plural = 'Agent turns'
        ordering = ['execution_id', 'index']
        unique_together = ['execution', 'index']
        indexes = [
            models.Index(fields=['execution', 'index']),
        ]

    def __str__(self):
        return f'Turn {self.index} of {self.execution_id} ({self.decision})'


class AgentStep(models.Model):
    """One tool call, belonging to the turn that issued it.

    Was `NodeExecutionLog` (table `logs_nodeexecutionlog`) until 2026-08-19,
    when the DAG vocabulary was retired: `node_id` was never a node id but a
    provider `call_id`, and `node_type` was the tool name.

    `execution` is kept alongside `turn` on purpose. The run is the unit
    everything else keys by — `ws/execution/{id}/`, `/api/logs/executions/{id}/`
    — and the socket replay in `streaming/consumers.py` wants every step of a run
    without a join through turns. A step whose turn row failed to write is also
    still a real step, and must stay reachable.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ]

    execution = models.ForeignKey(
        ExecutionLog, on_delete=models.CASCADE, related_name='steps'
    )
    #: Null only for a step written before its turn row existed.
    turn = models.ForeignKey(
        AgentTurn,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='steps',
    )

    #: The provider's tool-call id. It is what `approve_tool_call` resumes on,
    #: which is why it is stored rather than regenerated.
    call_id = models.CharField(max_length=100, help_text="The provider's tool-call id")
    tool = models.CharField(max_length=100, help_text='Tool name, e.g. web_search')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    order = models.IntegerField(
        default=0, help_text='Position within the run; survives a resume'
    )

    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    duration_ms = models.IntegerField(
        blank=True, null=True, validators=[MinValueValidator(0)]
    )

    args = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)

    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Agent step'
        verbose_name_plural = 'Agent steps'
        ordering = ['order']
        indexes = [
            models.Index(fields=['execution', 'call_id']),
            models.Index(fields=['execution', 'status']),
            models.Index(fields=['execution', 'order']),
            models.Index(fields=['turn', 'order']),
            models.Index(fields=['tool', 'status']),
        ]

    def __str__(self):
        return f"{self.tool} ({self.status})"
