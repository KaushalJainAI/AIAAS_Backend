"""Long-horizon missions: goals that outlive any single run. (P7)

A run is at most 2 h and ~40 iterations, and each run starts from zero.
A mission is carried out by a chain of ordinary runs, each through the one
door (`start_agent_run`), so guardrails, logging and spend caps apply
unchanged.

- `Mission`: goal, status, plan (json todos), notebook path, budget, deadline,
  max_runs, runs_done, wait_for, next_wake_at.
- `ExecutionLog.mission` FK joins every run in the chain.
- The plan outlives runs: loaded into each run's metadata.todos at start,
  written back at end.
- Notebook: `/Agents/<name>/missions/<id>/NOTES.md` — decisions, findings,
  what was tried. A file, not a column, so the user reads and edits it.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Mission(models.Model):
    """One multi-run goal."""

    STATUS_CHOICES = [
        ('active', 'Active'),
        ('waiting', 'Waiting'),
        ('paused', 'Paused'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='missions',
    )
    agent = models.ForeignKey(
        'orchestrator.SubAgent',
        on_delete=models.CASCADE,
        related_name='missions',
    )
    goal = models.TextField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='active')
    #: The plan, as todos. Loaded into each run, written back at end.
    plan = models.JSONField(default=list, blank=True)
    notebook_path = models.CharField(max_length=500, blank=True, default='')
    budget_inr = models.IntegerField(default=0)
    spent_inr = models.IntegerField(default=0)
    deadline = models.DateTimeField(null=True, blank=True)
    max_runs = models.IntegerField(default=20)
    runs_done = models.IntegerField(default=0)
    #: What the mission waits on: {event, filter, timeout}.
    wait_for = models.JSONField(default=dict, blank=True)
    next_wake_at = models.DateTimeField(null=True, blank=True)
    last_report = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status', 'next_wake_at']),
        ]

    def __str__(self):
        return f'mission {self.id} ({self.status}): {self.goal[:60]}'
