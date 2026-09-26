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
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class SubAgentRevision(models.Model):
    """One saved version of an agent's configuration.

    The snapshot is the flat `AgentConfig` dict that
    `agents.config.AgentSerializer.to_config` already produces for the
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
        # An earlier revision put back. A new row, never an edit of history.
        ('restore', 'Restored'),
    ]

    #: Null once the agent is deleted. The snapshot outlives it on purpose:
    #: the runs that executed under this configuration keep pointing at it, and
    #: a run whose config vanished can no longer say why it behaved as it did.
    subagent = models.ForeignKey(
        'orchestrator.SubAgent',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
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
        ('eval', 'Evaluation sweep'),
        ('mission', 'Mission chain'),
    ]

    execution_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text='Unique identifier for this execution',
    )
    # Null for runs whose agent has since been deleted, and for the historical
    # rows that belonged to a node graph — see migration 0009. SET_NULL, not
    # CASCADE: the column was always nullable for exactly this reason, and yet
    # deleting an agent erased every run it had made, with its spend.
    subagent = models.ForeignKey(
        'orchestrator.SubAgent',
        on_delete=models.SET_NULL,
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
    #: The mission this run belongs to, if it is one link in a chain.
    mission = models.ForeignKey(
        'missions.Mission',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='runs',
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

    #: The LangGraph checkpointer key for this run, promoted out of
    #: `input_data` into an indexed column.
    #:
    #: It was only ever stored inside the `input_data` JSON, and three hot
    #: paths looked it up there — resuming a paused run, closing a HITL request
    #: on approval or rejection, and resolving the parent step of a delegated
    #: run. On SQLite a `input_data__thread_id=` filter is a full table scan
    #: with a JSON parse per row, so the cost grew with every run the account
    #: had ever made, and it was paid on the two paths a person is actively
    #: waiting on: clicking approve, and delegating.
    #:
    #: `input_data['thread_id']` is still written, because it is what the
    #: historical rows carry and what the run's own record shows. This column
    #: is the *addressable* copy; `logs/models.py` is the only place that has
    #: to know they are the same string.
    thread_id = models.CharField(
        max_length=200, blank=True, default='', db_index=True,
        help_text='Checkpointer thread key; indexed copy of input_data.thread_id',
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
    #: Machine-readable failure kind (`logs/failures.py::classify`). Blank =
    #: unknown (old rows). Set at the single close point (`_close_log`) and in
    #: recovery, so a quality dashboard can count by category.
    FAILURE_CHOICES = [
        ('provider', 'Provider'),
        ('step_budget', 'Step budget'),
        ('tool_error', 'Tool error'),
        ('guardrail', 'Guardrail'),
        ('contract', 'Contract'),
        ('timeout', 'Timeout'),
        ('cancelled', 'Cancelled'),
        ('interrupted', 'Interrupted'),
        ('other', 'Other'),
    ]
    failure_category = models.CharField(max_length=16, blank=True, default='')

    # ── Resource usage ──
    nodes_executed = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    tokens_used = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    #: Dead column, kept only because dropping it is a separate migration on a
    #: live table. Nothing has ever written it; `cost_usd` below is the number
    #: the spend cap and the UI both read. Do not revive it.
    credits_used = models.IntegerField(default=0, validators=[MinValueValidator(0)])

    # ── Cost ──
    # The breakdown, because a single total cannot be priced: output costs 5-6x
    # input on every model in the registry and a cache read costs a tenth of
    # one, so `tokens_used` alone is wrong by up to an order of magnitude in
    # either direction. Summed from the run's turns rather than recomputed at
    # read time — a run can switch models on resume, so there is no single rate
    # to apply to a run-level total.
    input_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    output_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    cached_read_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    cached_write_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    #: USD, six decimal places: a cheap turn costs $0.0003 and rounding to
    #: four would floor a run of them to zero.
    cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, default=Decimal('0.000000'),
    )
    #: `billed` (the provider told us), `estimated` (our price table), or
    #: `unpriced` (we do not know). Stored rather than derived because the
    #: answer depends on what the price table said *at the time*, and because
    #: a zero that means "free" and a zero that means "unknown" must never
    #: render alike. See `llm/pricing.py`.
    cost_source = models.CharField(max_length=12, blank=True, default='')

    supervision_level = models.CharField(
        max_length=20, blank=True, help_text='Level of supervision used for this run'
    )
    #: The model value that actually served this run, e.g.
    #: `deepseek/deepseek-v4.1-flash`. Recorded per run rather than per turn
    #: (turns carry their own `model_id`): a run whose configured model was
    #: retired executes on the platform fallback, and "what did it run on" is
    #: the first question when such an answer is wrong. Blank on rows that
    #: predate fallback tracking.
    model_used = models.CharField(
        max_length=150, blank=True, default='',
        help_text='Model value that served this run',
    )
    #: The configured model value this run fell back *from*, e.g.
    #: `qwen/qwen3.8-max`. Blank when no substitution happened. The agent's
    #: own `llm_model` is deliberately left untouched — the config stays what
    #: the owner chose, while this column says what the run did about it.
    fallback_from = models.CharField(
        max_length=150, blank=True, default='',
        help_text='Configured model value substituted away from, if any',
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
    #: The same breakdown as the run carries, at the level it is actually
    #: knowable: one turn is one model call, on one model, at one set of rates.
    #: The run's totals are the sum of these. `input_tokens` excludes the
    #: cached buckets, so the four are disjoint and can be priced and added.
    input_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    output_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    cached_read_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    cached_write_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    #: Reasoning tokens, a subset of `output_tokens`. Never priced separately —
    #: providers bill it as output — but recorded because a run whose spend is
    #: all reasoning is a different problem from one whose spend is all answer.
    reasoning_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, default=Decimal('0.000000'),
    )
    cost_source = models.CharField(max_length=12, blank=True, default='')
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


class Feedback(models.Model):
    """Explicit judgement: thumbs up/down on a run or a chat message.

    Exactly one of `execution` / `chat_message` is set (CheckConstraint).
    Re-rating is an update, not a second row (partial UniqueConstraints).
    All rows cascade-delete with the user. Nothing here is sent to a model.
    """

    REASON_CHOICES = [
        ('wrong', 'Wrong'),
        ('incomplete', 'Incomplete'),
        ('slow', 'Slow'),
        ('unsafe', 'Unsafe'),
        ('ignored_instructions', 'Ignored instructions'),
        ('other', 'Other'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='feedbacks',
    )
    execution = models.ForeignKey(
        ExecutionLog, on_delete=models.CASCADE, null=True, blank=True,
        related_name='feedbacks',
    )
    chat_message = models.ForeignKey(
        'chat.ChatMessage', on_delete=models.CASCADE, null=True, blank=True,
        related_name='feedbacks',
    )
    rating = models.SmallIntegerField(help_text='+1 or -1')
    reason = models.CharField(max_length=24, blank=True, default='')
    comment = models.TextField(blank=True, max_length=2000, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Feedback'
        verbose_name_plural = 'Feedback'
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(execution__isnull=False, chat_message__isnull=True)
                    | models.Q(execution__isnull=True, chat_message__isnull=False)
                ),
                name='logs_feedback_exactly_one_target',
            ),
            models.UniqueConstraint(
                fields=['user', 'execution'],
                condition=models.Q(execution__isnull=False),
                name='logs_feedback_unique_user_execution',
            ),
            models.UniqueConstraint(
                fields=['user', 'chat_message'],
                condition=models.Q(chat_message__isnull=False),
                name='logs_feedback_unique_user_message',
            ),
        ]
        indexes = [
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f'{self.user_id}: {self.rating}'


class RunSignal(models.Model):
    """Implicit judgement: structured retry/cancel/steer/reject events.

    Best-effort telemetry — writers wrap in try/except and never fail the
    user's action. One writer (`logs/signals_api.py::record_signal`).
    """

    KIND_CHOICES = [
        ('regenerated', 'Regenerated'),
        ('steered', 'Steered'),
        ('steers_returned', 'Steers returned'),
        ('approval_rejected', 'Approval rejected'),
        ('approval_granted', 'Approval granted'),
        ('cancelled', 'Cancelled'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='run_signals',
    )
    execution = models.ForeignKey(
        ExecutionLog, on_delete=models.CASCADE, null=True, blank=True,
        related_name='signals',
    )
    chat_session_id = models.CharField(max_length=100, blank=True, default='')
    chat_message = models.ForeignKey(
        'chat.ChatMessage', on_delete=models.CASCADE, null=True, blank=True,
        related_name='signals',
    )
    kind = models.CharField(max_length=24)
    detail = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Run signal'
        verbose_name_plural = 'Run signals'
        indexes = [
            models.Index(fields=['user', 'kind', '-created_at']),
        ]

    def __str__(self):
        return f'{self.user_id} {self.kind}'


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

    #: How this call got past the gate under `auto`: {mode, verdict, reason,
    #: reviewed_by}. Null for calls that asked, were remembered, or ran read —
    #: the column records reviewer decisions, not every dispatch.
    approval = models.JSONField(null=True, blank=True)

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


class CostEntry(models.Model):
    """Non-token spend: images, messages, browser minutes, compute, transcription.

    Reading `AgentStep.result` for costs was right for one priced tool
    (`generate_image`). It does not scale to six: each tool would need its own
    parsing rule in the rollup, and a step re-run on resume would double-count
    what it re-records. So priced tools write here through `logs/costs.py::record`
    — the only writer — and `agents/spend.py` sums tokens + these rows for the
    spend cap. An unpriced call is estimated, never free.

    Either `execution` or `session` names where the spend happened; both null
    means platform-attributed (a calibration run, a reviewer call).
    """

    KIND_CHOICES = [
        ('image', 'Image generation'),
        ('sms', 'SMS'),
        ('whatsapp', 'WhatsApp'),
        ('browser', 'Browser minutes'),
        ('compute', 'Compute minutes'),
        ('transcription', 'Transcription'),
        ('esign', 'E-signature'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='cost_entries',
    )
    execution = models.ForeignKey(
        ExecutionLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cost_entries',
    )
    session = models.ForeignKey(
        'chat.ChatSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cost_entries',
    )
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    units = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0'))
    unit = models.CharField(max_length=16, blank=True, default='')
    #: Rupees, rounded up at write time — the spend cap is denominated in
    #: rupees, so the stored unit is the one the guardrail compares against.
    amount_inr = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    #: False only when a provider reported the charge. True means our estimate,
    #: which still counts — an unpriced call must never read as free.
    estimated = models.BooleanField(default=True)
    source = models.CharField(
        max_length=100, blank=True, default='',
        help_text='Tool or provider that produced the charge, e.g. generate_image',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Cost entry'
        verbose_name_plural = 'Cost entries'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['execution', '-created_at']),
        ]

    def __str__(self):
        return f"{self.kind} ₹{self.amount_inr} ({self.user_id})"
