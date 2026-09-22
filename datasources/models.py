"""
External data, reached generically instead of one connector per system.

`DataConnection` names a database; `ApiConnection` names an HTTP API with an
optional OpenAPI spec. Passwords and tokens are vault references
(`secret_ref`), never stored here — resolution happens at dispatch, after
approval, through `CredentialManager`. `sqlite` is the local kind: a database
file in the user's own tree, which is also what makes SQL testable without
any external service.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class DataConnection(models.Model):
    """One database the user's agents may query."""

    KIND_CHOICES = [
        ('postgres', 'PostgreSQL'),
        ('mysql', 'MySQL'),
        ('bigquery', 'BigQuery'),
        ('sqlite', 'SQLite file in your workspace'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='data_connections',
    )
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    name = models.CharField(max_length=120)
    host = models.CharField(max_length=253, blank=True, default='')
    port = models.IntegerField(null=True, blank=True)
    database = models.CharField(max_length=200, blank=True, default='')
    username = models.CharField(max_length=200, blank=True, default='')
    #: Vault reference (`secret_ref` form) for the password or key file.
    secret_ref = models.CharField(max_length=200, blank=True, default='')
    #: For sqlite: the VFS path of the database file.
    vfs_path = models.CharField(max_length=500, blank=True, default='')
    ssl_mode = models.CharField(max_length=20, blank=True, default='')
    allow_write = models.BooleanField(
        default=False,
        help_text='Offer execute_sql for this connection. Reads never need it.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'name'], name='unique_data_connection_per_user'),
        ]
        indexes = [models.Index(fields=['user', 'kind'])]

    def __str__(self):
        return f'{self.name} ({self.kind})'


class ApiConnection(models.Model):
    """One HTTP API the user's agents may call."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='api_connections',
    )
    name = models.CharField(max_length=120)
    base_url = models.URLField(max_length=1000)
    #: Optional OpenAPI document (parsed JSON). Calls are validated against it
    #: when present; without it, the egress guard and the method allowlist are
    #: the whole validation.
    openapi_spec = models.JSONField(default=dict, blank=True)
    #: {type: bearer|header|query|basic, secret_ref, header?, param?}.
    #: Any `Authorization` the model supplies is dropped at dispatch.
    auth = models.JSONField(default=dict, blank=True)
    allowed_methods = models.JSONField(
        default=list, blank=True,
        help_text='Uppercase methods this connection may use. Empty: GET, HEAD.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'name'], name='unique_api_connection_per_user'),
        ]
        indexes = [models.Index(fields=['user', 'name'])]

    def __str__(self):
        return f'{self.name} ({self.base_url})'
