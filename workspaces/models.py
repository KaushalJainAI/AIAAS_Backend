"""
One persistent, isolated Linux machine per user.

`execute_python` is a dead process per snippet; a pipeline, a test suite or a
scheduled job needs a place that keeps files, keeps packages and keeps
running. That place is a `Workspace`: one per user in v1, idle-hibernated
after 15 min, quotas enforced, destroyed on account deletion.

What never enters one: platform secrets. No `.env`, no DB credentials, no
`SECRET_KEY` — secrets arrive only as named references into a single
command's environment, resolved after approval. The workspace is the one
place in this platform where model-chosen commands run outside a denylist,
and the VM boundary (or the docker container standing in for it in dev) is
what makes that acceptable — not a list of forbidden words.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Workspace(models.Model):
    """One user's machine."""

    STATUS_CHOICES = [
        ('creating', 'Creating'),
        ('running', 'Running'),
        ('hibernated', 'Hibernated'),
        ('failed', 'Failed'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='workspace',
        help_text='One per user in v1. Destroyed with the account.',
    )
    #: The engine-side id (docker container name, provider id).
    provider_id = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='creating')
    disk_gb = models.IntegerField(default=5)
    #: `none` (no network) or `bridge` (default routes). The per-host
    #: allowlist (`workspaceEgress`) is enforced by the production provider;
    #: the docker engine honours `none` vs anything else.
    egress_policy = models.CharField(max_length=12, default='bridge')
    #: Webhook credential for job completion callbacks.
    secret = models.CharField(max_length=64, blank=True, default='')
    last_active_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'workspace of {self.user_id} ({self.status})'


class WorkspaceJob(models.Model):
    """One detached command: started by a run, finished on its own time."""

    STATUS_CHOICES = [
        ('running', 'Running'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name='jobs',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='workspace_jobs',
    )
    name = models.CharField(max_length=120)
    cmd = models.TextField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='running')
    exit_code = models.IntegerField(null=True, blank=True)
    #: Where the log lives: engine-side path, fetched on `job_logs`.
    log_path = models.CharField(max_length=500, blank=True, default='')
    timeout_s = models.IntegerField(default=6 * 3600)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['workspace', 'status']),
        ]

    def __str__(self):
        return f'job {self.id} ({self.status}): {self.name}'


class CodeProject(models.Model):
    """One repo checked out into the user's workspace."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='code_projects',
    )
    name = models.CharField(max_length=120)
    repo_url = models.CharField(max_length=500, blank=True, default='')
    default_branch = models.CharField(max_length=120, blank=True, default='main')
    workspace_path = models.CharField(
        max_length=500,
        help_text='Project root on the workspace disk, e.g. /home/user/projects/api',
    )
    github_secret_ref = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'name'], name='unique_code_project_per_user'),
        ]

    def __str__(self):
        return f'{self.name} ({self.user_id})'


class CodeChange(models.Model):
    """One file an agent run changed, so the UI shows and reverts per file."""

    run = models.ForeignKey(
        'logs.ExecutionLog',
        on_delete=models.CASCADE,
        related_name='code_changes',
    )
    path = models.CharField(max_length=500)
    before_hash = models.CharField(max_length=64, blank=True, default='')
    after_hash = models.CharField(max_length=64, blank=True, default='')
    diff = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.path} @ {self.run_id}'
