"""
The per-user overlay on a code-owned tool library.

Tools are code (`chat/tools/registry.py`) and stay that way: a user does not
author a tool, they decide which of the ones we ship their assistant may reach
and, for the handful that have a number worth moving, what that number is.

One row per (user, tool). **An absent row is the code default** — that is the
whole design. A fresh `migrate` yields zero rows and every tool behaves exactly
as it did before this app existed, so the overlay can never be the reason a
fresh install is broken, and "reset to default" is a DELETE rather than a second
notion of what default means.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ToolConfig(models.Model):
    """`enabled` is a kill switch; `config` holds the declared knobs.

    `config` is a JSON blob rather than columns because the knobs differ per
    tool and there are four of them — a column per knob would be a migration
    every time a tool grows a limit. It is not free-form: `settings_schema.py`
    declares what each tool accepts and the serializer rejects everything else,
    so an unknown key can never be stored and later read back as if it meant
    something.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='tool_configs',
    )
    #: Must name a tool in the registry. Validated on write, not on read: a
    #: renamed tool leaves a row that matches nothing, which is inert, while a
    #: read-time check would make every listing pay for the registry lookup.
    tool_name = models.CharField(max_length=64)
    enabled = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'tool_name')]
        indexes = [models.Index(fields=['user', 'enabled'])]
        verbose_name = 'tool configuration'

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        state = 'on' if self.enabled else 'off'
        return f'{self.tool_name} ({state})'
