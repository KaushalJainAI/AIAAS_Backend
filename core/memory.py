"""
Reading and writing what we know about a user.

Kept out of the tool module because two very different callers need it: the
tool that writes a fact, and the prompt builder that reads them all back on
every turn. Putting the read beside the write is what stops the two disagreeing
about ordering and caps.

The one rule that shapes everything here: **a fact is worth storing only if it
would change an answer later.** A store that fills with "the user said hello"
costs tokens on every single turn of every session for ever, and buries the
three facts that actually matter. That judgement cannot be enforced in code, so
it is stated hard in the tool description — and backed by caps, so a model with
bad judgement degrades the store slowly instead of destroying it.
"""
from __future__ import annotations

import logging

from .models import UserMemory

logger = logging.getLogger(__name__)

#: Characters of memory injected into one system prompt. A ceiling on the whole
#: block, not per fact, because the cost that matters is what rides on every
#: turn — and unlike the per-category caps this one is what the user pays for.
MAX_PROMPT_CHARS = 1_500


def remember(user, text: str, category: str = 'context', *,
             source: str = 'agent') -> tuple[UserMemory | None, bool]:
    """Store one fact, or refresh it if it is already known.

    Returns `(row, created)`. An exact repeat is a touch rather than an insert,
    which is what keeps the store from filling with the same fact in slightly
    different words every time a conversation revisits it — and touching it
    also protects it from the cap below, because a fact that keeps coming up is
    evidently one worth keeping.
    """
    text = (text or '').strip()
    if not text:
        return None, False
    text = text[:500]

    valid = {key for key, _ in UserMemory.CATEGORIES}
    if category not in valid:
        category = 'context'

    row, created = UserMemory.objects.get_or_create(
        user=user, text=text,
        defaults={'category': category, 'source': source},
    )
    if not created:
        # `save()` rather than `update()` so `auto_now` fires: recency is what
        # the cap evicts on, so a repeated fact has to move to the front.
        row.category = category
        row.save(update_fields=['category', 'updated_at'])
    else:
        _enforce_cap(user, category)
    return row, created


def _enforce_cap(user, category: str) -> None:
    """Drop the least recently touched facts in one category past the cap.

    Per category, not overall: a burst of new project facts must not evict who
    the user is. Least-recently-*touched* rather than oldest-created, because
    `remember` refreshes on repeat — so the survivors are the facts that keep
    proving relevant rather than merely the newest ones.
    """
    ids = list(
        UserMemory.objects
        .filter(user=user, category=category)
        .order_by('-updated_at')
        .values_list('id', flat=True)[UserMemory.MAX_PER_CATEGORY:]
    )
    if ids:
        UserMemory.objects.filter(id__in=ids).delete()
        logger.info('[Memory] Evicted %d stale %s memories for user %s',
                    len(ids), category, getattr(user, 'id', None))


def forget(user, text: str) -> int:
    """Remove a fact by its exact text. Returns how many rows went."""
    text = (text or '').strip()
    if not text:
        return 0
    deleted, _ = UserMemory.objects.filter(user=user, text__iexact=text).delete()
    return deleted


def for_prompt(user_id: int | None) -> str:
    """Everything known about this user, rendered for the system prompt, or ''.

    Goes in the **system message**, not the per-turn context update, and that
    is a deliberate exception to the rule the clock taught: this changes only
    when a fact is written, which is rare, while the clock changed on every
    single turn. Session-stable is the bar for the cached prefix, and this
    clears it.

    Grouped by category so the model reads a shape rather than a list, and
    bounded as a whole because the block is paid for on every turn of every
    session.
    """
    if not user_id:
        return ''

    rows = list(
        UserMemory.objects
        .filter(user_id=user_id)
        .order_by('category', '-updated_at')
        .values_list('category', 'text')[:200]
    )
    if not rows:
        return ''

    labels = dict(UserMemory.CATEGORIES)
    grouped: dict[str, list[str]] = {}
    for category, text in rows:
        grouped.setdefault(category, []).append(text)

    lines = ['### WHAT YOU KNOW ABOUT THIS USER ###']
    for category, items in grouped.items():
        lines.append(f'{labels.get(category, category)}:')
        lines.extend(f'- {text}' for text in items)

    block = '\n'.join(lines)
    if len(block) > MAX_PROMPT_CHARS:
        # Cut whole lines, never mid-fact: half a sentence about the user reads
        # as a fact in its own right and can be flatly wrong.
        kept: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > MAX_PROMPT_CHARS:
                break
            kept.append(line)
            size += len(line) + 1
        block = '\n'.join(kept)
    return block
