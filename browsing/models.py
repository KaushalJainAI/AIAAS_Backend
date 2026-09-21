"""
A persistent browser profile per user per domain.

The browser was stateless: each call was one remote function, so a login never
survived and anything behind auth was unreachable. A session keeps the
provider's profile (cookies, local storage) for one user on one registrable
domain — never shared across users — so a portal login lasts long enough to do
the job. Idle 10 min, hard cap 60 min; the sweep closes the rest.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class BrowserSession(models.Model):
    """One user's logged-in browser on one domain."""

    STATUS_CHOICES = [
        ('open', 'Open'),
        ('closed', 'Closed'),
        ('expired', 'Expired'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='browser_sessions',
    )
    domain = models.CharField(
        max_length=255,
        help_text='Registrable domain this profile belongs to, e.g. example.com',
    )
    provider_session_id = models.CharField(
        max_length=255, blank=True, default='',
        help_text='The provider-side session, when the engine supports reconnect',
    )
    live_url = models.URLField(
        max_length=1000, blank=True, default='',
        help_text='Provider live-view URL, when one was offered',
    )
    live_expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='open')
    last_used_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(
        help_text='Hard cap: the provider profile is dropped whatever is happening',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['user', 'domain', 'status']),
        ]

    def __str__(self):
        return f'{self.user_id}@{self.domain} ({self.status})'
