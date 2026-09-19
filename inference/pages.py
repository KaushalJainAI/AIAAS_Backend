"""
Publishing a hosted page: the one write path, shared by the API and the tool.

`POST /api/inference/pages/` and the `publish_page` tool used to carry their own
copy of this — slug minting, the file snapshot, the validation — with different
size caps, which is how a page the API refused could still be published by an
agent. One function now, the same rule `AgentSerializer` follows for agents: a
second write path is a second place for a check to be forgotten.
"""
from __future__ import annotations

import json

from django.utils.text import slugify

from workflow_backend.thresholds import AGENT_FILE_BINARY_BYTES, PUBLISHED_PAGE_BODY_CHARS

KINDS = ('report', 'html', 'file')
VISIBILITIES = ('link', 'platform', 'public')


class PublishError(ValueError):
    """Written for whoever asked — a person in the UI or a model in a run."""


def mint_slug(title: str) -> str:
    from .models import PublishedPage

    base = slugify(title)[:180] or 'page'
    candidate, counter = base, 1
    while PublishedPage.objects.filter(slug=candidate).exists():
        counter += 1
        candidate = f'{base}-{counter}'
    return candidate


def publish(user, *, title: str, kind: str, body, visibility: str):
    """Freeze a snapshot and return the saved `PublishedPage`."""
    from django.core.files.base import ContentFile

    from .models import Document, PublishedPage

    title = (title or '').strip()[:200]
    kind = (kind or 'report').strip()
    visibility = (visibility or 'platform').strip()
    if not title:
        raise PublishError('A title is required.')
    if kind not in KINDS:
        raise PublishError(f'Unknown kind "{kind}". Use one of: {", ".join(KINDS)}.')
    if visibility not in VISIBILITIES:
        raise PublishError(f'Unknown visibility "{visibility}". Use one of: {", ".join(VISIBILITIES)}.')
    if not isinstance(body, str):
        raise PublishError('Body must be text.')
    if len(body) > PUBLISHED_PAGE_BODY_CHARS:
        raise PublishError(
            f'That is {len(body):,} characters; the limit for one page is '
            f'{PUBLISHED_PAGE_BODY_CHARS:,}. Split it or shorten it.'
        )

    page = PublishedPage(owner=user, slug=mint_slug(title), title=title, kind=kind,
                         body=body, visibility=visibility, is_listed=True)
    if kind == 'file':
        try:
            payload = json.loads(body) if body.strip() else {}
        except ValueError as exc:
            raise PublishError('A file page body must be JSON like {"document_id": 12}.') from exc
        document = Document.objects.filter(
            user=user, id=(payload or {}).get('document_id') if isinstance(payload, dict) else None,
        ).first()
        if document is None:
            raise PublishError('No such file in your documents.')
        if document.file:
            if (document.file_size or 0) > AGENT_FILE_BINARY_BYTES:
                raise PublishError('That file is too large to publish.')
            with document.file.open('rb') as handle:
                data = handle.read(AGENT_FILE_BINARY_BYTES + 1)
            page.file.save(document.name, ContentFile(data), save=False)
        else:
            # A text document an agent wrote has no bytes of its own: the text
            # is the file, so that is what the snapshot keeps.
            page.file.save(document.name, ContentFile((document.content_text or '').encode('utf-8')),
                           save=False)
        page.file_name = document.name
    page.save()
    return page
