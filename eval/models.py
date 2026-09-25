"""
Is this agent any good, and who says so?

An evaluation is the other half of `logs/`. The logs answer *what an agent
did*; these tables answer *whether it should have done that* — and, because a
grader is a program with opinions, *whether the grader was right*.

    EvalSuite -- EvalCase
        |
        +-- EvalRun -- EvalResult -- EvalReview
        (one sweep)   (one case)    (a human's verdict)

- `EvalSuite` — a named set of cases, plus the supervision policy that decides
  which of its results a person is asked to look at.
- `EvalCase` — one goal handed to the agent, and the graders its answer is held
  to.
- `EvalRun` — one sweep of a suite against one agent, pinned to the exact
  `SubAgentRevision` it ran under.
- `EvalResult` — one case's outcome, pointing at the `ExecutionLog` it produced
  so the full turn-by-turn trace stays one hop away.
- `EvalReview` — a human verdict on a result.

**Why the human verdict is a separate row rather than a column.** A reviewer
does not overwrite the grader; they *disagree with it*, and the disagreement is
the most valuable number here. Keeping `auto_passed` and the review side by side
is what makes `EvalRun.grader_agreement` computable — a suite whose graders
agree with people 55% of the time is measuring nothing, and no column that
overwrote itself could ever tell you that.

**Why a run pins a revision.** Same reason `ExecutionLog` does: an agent edited
while a suite is sweeping must not retroactively change what the sweep claims to
have scored. See `logs/models.py::SubAgentRevision`.
"""
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.text import slugify


class EvalSuite(models.Model):
    """A named set of cases, and the policy for who checks the checker."""

    #: Which results a person is asked to look at once the graders have run.
    #:
    #: `disagreement` is the default worth reasoning about: it queues exactly
    #: the results the automatic graders were least sure of — a split verdict
    #: across graders, or an LLM judge parked in the middle of its range. That
    #: is where a grader is most likely to be wrong, and it is the only policy
    #: whose review cost does not grow with the size of the suite.
    SUPERVISION_CHOICES = [
        ('none', 'No human review'),
        ('failures', 'Review every failure'),
        ('disagreement', 'Review where the graders are unsure'),
        ('sampled', 'Review a random sample'),
        ('all', 'Review everything'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='eval_suites',
    )

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, blank=True)
    description = models.TextField(blank=True)

    #: The agent this suite is written for. Nullable because a suite can be
    #: generic ("does any support agent stay polite?") and run against several;
    #: `EvalRun.subagent` records which one a given sweep actually scored.
    subagent = models.ForeignKey(
        'orchestrator.SubAgent',
        on_delete=models.SET_NULL, null=True, blank=True, related_name='eval_suites',
    )

    # -- Scoring --
    pass_threshold = models.FloatField(
        default=0.8,
        help_text='Weighted fraction of cases that must pass for the run to pass',
    )

    # -- Supervision --
    supervision = models.CharField(
        max_length=16, choices=SUPERVISION_CHOICES, default='disagreement',
    )
    sample_percent = models.IntegerField(
        default=20, validators=[MinValueValidator(0)],
        help_text="Percentage of results queued when supervision is 'sampled'",
    )
    #: Who is asked. Null means the suite's owner. A column rather than
    #: "whoever is logged in" because an unattended sweep has nobody logged in,
    #: and a review queue with no addressee is a queue nobody reads.
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='eval_suites_to_review',
    )

    # -- Execution --
    #: What an eval run does with a call that would have paused for approval
    #: under the agent's own autonomy. Either way it is recorded as an intent
    #: and nothing waits: `run` executes it for real (the default — the run
    #: carries on as if approved), `block` declines it, for suites proving the
    #: gate fires, where running the call is exactly what must not happen.
    GATED_CALL_CHOICES = [('run', 'Record and run'), ('block', 'Record and block')]
    gated_calls = models.CharField(max_length=8, choices=GATED_CALL_CHOICES, default='run')
    concurrency = models.IntegerField(
        default=2, validators=[MinValueValidator(1)],
        help_text='Cases run in parallel. Capped by EVAL_MAX_CONCURRENCY.',
    )
    #: Sweep cost ceiling in rupees (null = unlimited). Checked in
    #: `runner._run_case` after acquiring the semaphore: a benchmark must never
    #: exhaust a user's real agent budget, so eval spend is excluded from the
    #: agent spend cap and bounded here instead.
    max_cost_rupees = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Sweep cost ceiling in rupees (agent + judge). Null = unlimited.',
    )

    tags = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)

    #: Which starter template this suite was cloned from, if any
    #: (`eval/starter_kits.py` key, e.g. 'research'). Null for hand-made
    #: suites. Used by the Evals page to offer "clone starter for my agent".
    template_slug = models.CharField(max_length=100, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Eval suite'
        verbose_name_plural = 'Eval suites'
        ordering = ['-updated_at']
        unique_together = ['user', 'name']
        indexes = [
            models.Index(fields=['user', '-updated_at']),
            models.Index(fields=['subagent', '-updated_at']),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def reviewer_or_owner_id(self) -> int:
        return self.reviewer_id or self.user_id


class EvalCase(models.Model):
    """One goal given to the agent, and what its answer is held to."""

    suite = models.ForeignKey(EvalSuite, on_delete=models.CASCADE, related_name='cases')

    name = models.CharField(max_length=200, blank=True)
    order = models.IntegerField(default=0)

    goal = models.TextField(help_text='The prompt the agent is run against')
    input_data = models.JSONField(
        default=dict, blank=True, help_text='Extra context passed alongside the goal',
    )

    #: What a good answer looks like, in prose. Read by the `llm_judge` grader
    #: and shown to a human reviewer — the same text serving both, because a
    #: rubric a person cannot apply is not one a judge should be trusted with.
    reference = models.TextField(blank=True)

    #: `[{"type": "contains", "value": "...", "weight": 1}, ...]`. Validated
    #: against `eval.graders.REGISTRY` on write, so an unknown grader is a 400
    #: rather than a case that silently scores 1.0 for ever.
    graders = models.JSONField(default=list, blank=True)

    weight = models.FloatField(default=1.0, help_text='Relative weight in the suite score')
    tags = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    #: The world version this case was built for (`EvalWorld.version`). Null
    #: for cases predating worlds — they run on whatever the live world is.
    #: A case whose version is not the live one is stale: kept, listed, and
    #: never swept until the world is regenerated around it or it is rebuilt.
    world_version = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Eval case'
        verbose_name_plural = 'Eval cases'
        ordering = ['order', 'id']
        indexes = [
            models.Index(fields=['suite', 'order']),
            models.Index(fields=['suite', 'is_active']),
        ]

    def __str__(self):
        return self.name or f'Case {self.pk}'


class EvalWorld(models.Model):
    """One fake situation a suite's cases share (`docs/EVAL_ENVIRONMENTS_PLAN.md`).

    The judge invents a scenario, plants ground-truth `facts`, and builds
    `fixtures` containing exactly those facts; cases are then written *about*
    the world with expected answers pointing at the facts. It belongs to a
    **suite, not a case**: one world with 12 cases is cheaper to build and lets
    cases refer to each other's data.

    A regenerated world is a **new version**, never an edit: runs name the
    version they used (`EvalRun.world_version`) and cases name the version
    they were built for (`EvalCase.world_version`), so scores from different
    worlds are never compared as if they were the same. Regenerating
    invalidates the old version's cases — they stay on their version rather
    than being deleted, because deleting them would rewrite what old sweeps
    scored.

    `status` is `draft` until a person accepts it **on the Evals page only** —
    the same provisional-until-asked rule as generated cases. A sweep runs
    only accepted cases on an accepted world.
    """

    #: `generating` while the judge builds it in the background (a world is
    #: five or so reasoning-model calls — too long to hold a request open);
    #: `failed` if that did not produce a usable world, with the reason in
    #: `error_message`. Neither is ever live: `live_world` reads `accepted`.
    STATUS_CHOICES = [
        ('generating', 'Generating'),
        ('failed', 'Failed'),
        ('draft', 'Draft'),
        ('accepted', 'Accepted'),
    ]

    suite = models.ForeignKey(EvalSuite, on_delete=models.CASCADE, related_name='worlds')
    #: Regenerations increment; the suite's live world is its newest `accepted`.
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='draft')
    #: Why generation failed, in words the Evals page shows. Blank otherwise.
    error_message = models.TextField(blank=True)
    #: What the generation was asked for, kept so "retry" repeats it.
    focus = models.CharField(max_length=500, blank=True)
    requested_cases = models.PositiveIntegerField(default=12)
    #: Cases the pipeline threw out, with reasons — shown beside the drafts.
    rejected = models.JSONField(default=list, blank=True)

    #: "Acme Tools, 40 staff, Q3 close in progress" — shown to the reviewer.
    brief = models.TextField(blank=True)
    #: Which surfaces exist: `{"files": true, "mail": true, ...}`. Only the
    #: surfaces the suite agent's grants can reach are ever built.
    surfaces = models.JSONField(default=dict, blank=True)
    #: The data itself, per surface (size-capped; see `eval/environment.py`).
    #: Files: `{path: text}`. Mail: `{messages: [...], labels: [...]}`.
    #: Calendar: `{events: [...]}`. Drive: `{files: [...], sheets: {...}}`.
    #: KB: `{documents: [{name, text}]}`. Web: `{pages: [...], results: {...}}`.
    fixtures = models.JSONField(default=dict, blank=True)
    #: The ground truth the judge planted, as `[{"key": ..., "statement": ...}]`.
    #: Every expected answer points at entries here rather than guessing anew.
    facts = models.JSONField(default=list, blank=True)

    created_by_model = models.CharField(max_length=200, blank=True)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Eval world'
        verbose_name_plural = 'Eval worlds'
        ordering = ['suite', 'version']
        constraints = [
            models.UniqueConstraint(
                fields=['suite', 'version'], name='unique_world_version_per_suite'),
        ]
        indexes = [
            models.Index(fields=['suite', '-version']),
        ]

    def __str__(self):
        return f'{self.suite_id} world v{self.version} ({self.status})'


class EvalRun(models.Model):
    """One sweep of a suite against one agent."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        # Every case has been graded, but a person still has to look at some of
        # them. Deliberately not 'completed': a run whose score can still move
        # must not be reported as final.
        ('awaiting_review', 'Awaiting review'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    run_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    suite = models.ForeignKey(EvalSuite, on_delete=models.CASCADE, related_name='runs')
    subagent = models.ForeignKey(
        'orchestrator.SubAgent',
        on_delete=models.SET_NULL, null=True, blank=True, related_name='eval_runs',
    )
    #: The configuration the agent was in when this sweep ran, pinned at open
    #: time. Without it, "it scored 0.9 last week" names nothing you can go
    #: back to.
    revision = models.ForeignKey(
        'logs.SubAgentRevision',
        on_delete=models.SET_NULL, null=True, blank=True, related_name='eval_runs',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='eval_runs',
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    #: The policy in force for *this* sweep, copied off the suite. Editing a
    #: suite must not rewrite the history of what was supervised.
    supervision = models.CharField(max_length=16, default='disagreement')

    total_cases = models.IntegerField(default=0)
    passed_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
    pending_review_count = models.IntegerField(default=0)

    #: Weighted fraction in [0, 1], human verdicts overriding grader verdicts.
    score = models.FloatField(null=True, blank=True)
    passed = models.BooleanField(null=True, blank=True)

    #: Fraction of *reviewed* results where the human agreed with the graders.
    #: Null until something has been reviewed. This is the number that says
    #: whether the suite is measuring anything.
    grader_agreement = models.FloatField(null=True, blank=True)

    tokens_used = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(
        null=True, blank=True, validators=[MinValueValidator(0)],
    )
    #: Accepted as the reference score for (suite, provider/model). Set by
    #: `manage.py benchmark accept`; one baseline set per suite+model.
    is_baseline = models.BooleanField(default=False, db_index=True)
    #: `agent` or `bare` (the platform-tax control). Stored so a report can say
    #: which half of an external comparison a run belongs to.
    mode = models.CharField(
        max_length=8,
        choices=[('agent', 'Agent'), ('bare', 'Bare model')],
        default='agent',
    )
    #: The `EvalWorld.version` this sweep ran on, or null for suites with no
    #: world. Recorded so scores from different worlds are never compared as
    #: if they were the same.
    world_version = models.IntegerField(null=True, blank=True)

    error_message = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Eval run'
        verbose_name_plural = 'Eval runs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['suite', '-created_at']),
            models.Index(fields=['subagent', '-created_at']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['revision', '-created_at']),
        ]

    def __str__(self):
        return f'{self.suite_id} sweep {self.run_id} ({self.status})'

    @property
    def is_complete(self) -> bool:
        return self.status in ('completed', 'failed', 'cancelled')


class EvalResult(models.Model):
    """One case's outcome inside a sweep."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('graded', 'Graded'),
        # The agent run itself failed — no credential, a refused guardrail, a
        # provider outage. Kept apart from a graded failure because "the agent
        # answered badly" and "the agent never answered" call for different
        # actions, and averaging them together hides outages as low scores.
        ('error', 'Errored'),
        ('skipped', 'Skipped'),
    ]

    REVIEW_STATES = [
        ('not_required', 'Not required'),
        ('pending', 'Awaiting review'),
        ('reviewed', 'Reviewed'),
    ]

    run = models.ForeignKey(EvalRun, on_delete=models.CASCADE, related_name='results')
    #: SET_NULL, with the goal copied below: deleting a case must not rewrite
    #: the history of the sweeps that scored it.
    case = models.ForeignKey(
        EvalCase, on_delete=models.SET_NULL, null=True, blank=True, related_name='results',
    )
    case_name = models.CharField(max_length=200, blank=True)
    goal = models.TextField(blank=True)

    #: The agent run this case produced. One hop to the full turn-by-turn
    #: trace, so nothing about *how* the answer was reached is duplicated here.
    execution = models.ForeignKey(
        'logs.ExecutionLog',
        on_delete=models.SET_NULL, null=True, blank=True, related_name='eval_results',
    )

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='pending')

    answer = models.TextField(blank=True)
    answer_truncated = models.BooleanField(default=False)

    # -- The graders' verdict --
    auto_passed = models.BooleanField(null=True, blank=True)
    auto_score = models.FloatField(default=0.0)
    #: One entry per grader: `{type, passed, score, weight, detail}`.
    grades = models.JSONField(default=list, blank=True)
    weight = models.FloatField(default=1.0)

    # -- Supervision --
    review_state = models.CharField(
        max_length=14, choices=REVIEW_STATES, default='not_required',
    )
    review_reason = models.CharField(
        max_length=120, blank=True,
        help_text='Why this landed in the queue, e.g. "graders disagreed"',
    )

    tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    #: What the `llm_judge` calls for this case cost. The agent answer is
    #: priced on its `ExecutionLog`; the judge was billed to the same key but
    #: no run recorded it — so it is recorded here and summed in `_finish`.
    judge_tokens = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    judge_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True,
    )
    duration_ms = models.IntegerField(
        null=True, blank=True, validators=[MinValueValidator(0)],
    )
    error_message = models.TextField(blank=True)
    #: What the run wanted from a person — approvals it would have paused for
    #: and `ask_user` questions (`agents.agent.runtime.collect_intents`). An
    #: eval never pauses; this is where the pause it would have made is kept.
    intents = models.JSONField(default=list, blank=True)
    #: What the agent changed in the eval world: emails sent, events created
    #: or deleted, files written, drive cells updated (`environment.snapshot`).
    #: The "what changed" panel reads this, so a reviewer sees what the agent
    #: *did*, not only what it said.
    env_changes = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Eval result'
        verbose_name_plural = 'Eval results'
        ordering = ['id']
        indexes = [
            models.Index(fields=['run', 'status']),
            models.Index(fields=['run', 'review_state']),
            # Backs the review queue: "everything waiting on me, newest first".
            models.Index(fields=['review_state', '-created_at']),
            models.Index(fields=['case', '-created_at']),
        ]

    def __str__(self):
        return f'{self.case_name or self.case_id} ({self.status})'

    # A human verdict wins, and is *not* copied into `auto_passed` — the
    # disagreement between the two is the point. See the module docstring.
    @property
    def final_passed(self):
        review = getattr(self, 'review', None)
        if review is not None and review.verdict in ('pass', 'fail'):
            return review.verdict == 'pass'
        return self.auto_passed

    @property
    def final_score(self) -> float:
        review = getattr(self, 'review', None)
        if review is not None and review.verdict in ('pass', 'fail'):
            return 1.0 if review.verdict == 'pass' else 0.0
        return self.auto_score


class JudgeCalibration(models.Model):
    """How the `llm_judge` scores against known labels.

    Platform-wide (not per user): the judge model is shared, so its agreement
    rate is read-only for everyone. `source='handwritten'` is the 30-row
    hand-built set; `source='gold'` rows come from external datasets with gold
    answers (Phase 8.1).
    """

    SOURCE_CHOICES = [
        ('handwritten', 'Hand-written'),
        ('gold', 'Gold answers'),
    ]

    judge_provider = models.CharField(max_length=30, default='openrouter')
    judge_model = models.CharField(max_length=200, blank=True)
    n = models.IntegerField(default=0)
    agreement = models.FloatField(default=0.0)
    false_pass_rate = models.FloatField(default=0.0)
    false_fail_rate = models.FloatField(default=0.0)
    details = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default='handwritten')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Judge calibration'
        verbose_name_plural = 'Judge calibrations'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source', '-created_at']),
        ]

    def __str__(self):
        return f'{self.source} {self.judge_model}: {self.agreement:.0%}'


class EvalReview(models.Model):
    """A person's verdict on one result.

    One per result (`OneToOne`): a second opinion is a different feature, and
    silently averaging two reviewers would make "the human said so" ambiguous
    exactly where it must not be.
    """

    VERDICT_CHOICES = [
        ('pass', 'Passed'),
        ('fail', 'Failed'),
        # Recorded rather than refused. An honest "I cannot tell" leaves the
        # grader's verdict standing but marks the case as one whose rubric is
        # not decidable — a finding about the suite, not a non-answer.
        ('unsure', 'Cannot tell'),
    ]

    result = models.OneToOneField(
        EvalResult, on_delete=models.CASCADE, related_name='review',
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True, related_name='eval_reviews',
    )

    verdict = models.CharField(max_length=8, choices=VERDICT_CHOICES)
    #: Computed on write, never supplied by the client. Null when the verdict
    #: is `unsure` (there is nothing to agree or disagree with) or when the
    #: graders reached no verdict at all.
    agreed_with_graders = models.BooleanField(null=True, blank=True)

    comment = models.TextField(blank=True)
    #: What the agent should have said. The one field here worth more than the
    #: verdict: a rubric the case did not have, written by someone looking at a
    #: real answer.
    corrected_answer = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Eval review'
        verbose_name_plural = 'Eval reviews'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['reviewer', '-created_at']),
            models.Index(fields=['verdict', '-created_at']),
        ]

    def __str__(self):
        return f'{self.result_id}: {self.verdict}'
