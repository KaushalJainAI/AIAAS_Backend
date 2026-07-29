"""
Evals — does the agent still do the right thing after you change something?

The score is the headline but not the point. The number that decides whether you
ship is **regressions**: cases that passed on the previous run and fail on this
one. An average can rise while the five cases you actually care about break, so
`EvalRun` stores its predecessor's per-case outcomes implicitly (via the run
chain) and the API computes the diff rather than trusting the aggregate.
"""
from django.conf import settings
from django.db import models


class EvalSuite(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='eval_suites'
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # What is being graded. Null means "whatever model the run names" — useful
    # for comparing two models on the same cases.
    agent = models.ForeignKey(
        'orchestrator.Workflow',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='eval_suites',
    )
    dataset = models.ForeignKey(
        'datasets.Dataset',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='eval_suites',
        help_text='Cases can be drawn from a dataset instead of written by hand',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        unique_together = ['user', 'name']
        indexes = [models.Index(fields=['user', '-updated_at'])]

    def __str__(self):
        return self.name

    @property
    def case_count(self):
        return self.cases.count()

    @property
    def latest_run(self):
        return self.runs.order_by('-created_at').first()


class EvalCase(models.Model):
    suite = models.ForeignKey(EvalSuite, on_delete=models.CASCADE, related_name='cases')
    # A stable human key ("inv-024") so a case keeps its identity across runs
    # and across edits to its wording — regressions are matched on this.
    key = models.CharField(max_length=64)
    description = models.CharField(max_length=300, blank=True, help_text='What makes this case hard')
    inputs = models.JSONField(default=dict)
    expected = models.JSONField(default=dict)
    weight = models.IntegerField(default=1, help_text='Relative importance when scoring')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['key']
        unique_together = ['suite', 'key']
        indexes = [models.Index(fields=['suite', 'key'])]

    def __str__(self):
        return f'{self.suite.name}:{self.key}'


class EvalRun(models.Model):
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    suite = models.ForeignKey(EvalSuite, on_delete=models.CASCADE, related_name='runs')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='eval_runs'
    )

    # Recorded per run, not read from the suite: the whole use of an eval is
    # comparing two configurations, so the configuration has to be part of the
    # result rather than something you look up afterwards and get wrong.
    provider = models.CharField(max_length=30, blank=True)
    model = models.CharField(max_length=100, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', db_index=True)
    total_cases = models.IntegerField(default=0)
    passed_cases = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['suite', '-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f'{self.suite.name} run {self.pk}'

    @property
    def score(self):
        """Percentage passed. Derived, never stored — a stored score can drift
        from the results it claims to summarise."""
        if not self.total_cases:
            return 0.0
        return round(self.passed_cases / self.total_cases * 100, 1)

    def previous(self):
        """The run before this one on the same suite."""
        return (
            EvalRun.objects
            .filter(suite=self.suite, status='completed', created_at__lt=self.created_at)
            .order_by('-created_at')
            .first()
        )

    def regressions(self):
        """Cases that passed last time and fail now.

        The honest signal. A rising average can hide these entirely, which is
        why this is computed per case rather than read off the score.
        """
        prev = self.previous()
        if not prev:
            return []
        was_passing = set(
            prev.results.filter(passed=True).values_list('case__key', flat=True)
        )
        now_failing = set(
            self.results.filter(passed=False).values_list('case__key', flat=True)
        )
        return sorted(was_passing & now_failing)


class EvalCaseResult(models.Model):
    run = models.ForeignKey(EvalRun, on_delete=models.CASCADE, related_name='results')
    case = models.ForeignKey(EvalCase, on_delete=models.CASCADE, related_name='results')

    got = models.JSONField(default=dict, help_text='What the model actually produced')
    passed = models.BooleanField(default=False)
    reason = models.TextField(blank=True, help_text='Why it was marked pass or fail')
    duration_ms = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['case__key']
        unique_together = ['run', 'case']
        indexes = [models.Index(fields=['run', 'passed'])]

    def __str__(self):
        return f'{self.case.key}: {"pass" if self.passed else "fail"}'
