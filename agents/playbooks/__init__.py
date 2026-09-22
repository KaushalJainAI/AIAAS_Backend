"""
Playbooks that travel with a template.

Templates travel without ids, so they cannot carry `Skill` rows. Coding
playbooks ship as code instead: `agents/playbooks/code/*.md`, loaded by slug.
The config gains `playbooks: [...]`, and the prompt builder appends them after
the brief, in the session-stable system prompt. Static text is allowed there;
per-run state is not.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Slugs a template may name. Closed so a typo fails loudly at save time
#: rather than silently dropping the playbook the template was written around.
PLAYBOOK_SLUGS = frozenset({
    'small-diffs',
    'run-tests-before-claiming-done',
    'read-before-edit',
    'python-testing',
    'ts-react',
    'git-hygiene',
})

_BASE = Path(__file__).resolve().parent / 'code'


def load(slug: str) -> str:
    """The markdown body for `slug`, or '' when it cannot be read."""
    name = (slug or '').strip()
    if name not in PLAYBOOK_SLUGS:
        return ''
    try:
        return (_BASE / f'{name}.md').read_text(encoding='utf-8').strip()
    except OSError:
        logger.warning('[Playbooks] Could not read playbook %s', name)
        return ''


def load_many(slugs) -> list[tuple[str, str]]:
    """`[(slug, body)]` for each readable slug, in order, de-duplicated."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for raw in slugs or []:
        slug = str(raw or '').strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        body = load(slug)
        if body:
            out.append((slug, body))
    return out
