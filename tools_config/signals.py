"""Write-through invalidation: a toggle takes effect on the next turn, not in a minute."""
from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import ToolConfig
from .overlay import invalidate


@receiver(post_save, sender=ToolConfig)
@receiver(post_delete, sender=ToolConfig)
def _invalidate(sender, instance: ToolConfig, **kwargs) -> None:
    invalidate(instance.user_id)
