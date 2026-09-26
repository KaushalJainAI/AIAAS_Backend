"""
Recently opened files and saved app tabs: the parts of a desktop that remember.

Two records, both per user and both small.

* **`RecentFile`**: one row per (user, document), moved to the top each time
  the file is opened. `Document.updated_at` answers "what changed lately"; this
  answers "what was I working on", which is what a Recent list on a computer
  means. It also carries `view_state` (a PDF's page, a zoom level) so a file
  reopens where it was left.
* **`AppSession`**: one row per (user, app) holding that app's open tabs, so
  closing the browser no longer forgets them.

Three rules carry it.

**Readability is checked on the way in and again on the way out.** Recording
an open uses the same predicate as reading the file (`_readable`: the owner's,
or one shared into the public library), so a row can never be minted for
someone else's private file. And a listing re-applies it, together with
"not trashed" and "not in the hidden eval tree", because a file can be
trashed or unshared after it was opened. A stale row is filtered, never shown
and never trusted.

**Everything is bounded.** `RECENT_CAP` rows per user (the oldest are dropped
on write), `MAX_TABS` per app, and `view_state` limited in size and to flat
scalar values. It is written by the browser on every page turn, so an
unbounded bag would be a free per-user blob store.

**An app is a label, not a key.** The app catalogue is frontend code
(`lib/apps.ts`), so the backend checks only the shape of the id. Rejecting
unknown apps here would mean every new app needs a backend deploy first.
"""
from __future__ import annotations

import json
import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import filesystem as fs
from .models import AppSession, Document, RecentFile

#: Rows kept per user. Past this the least recently opened are dropped.
RECENT_CAP = 100
#: Most a listing returns in one response.
LIST_LIMIT = 50
#: Tabs kept per app, matching the frontend's cap.
MAX_TABS = 20
#: Serialized size limit for one file's `view_state`.
VIEW_STATE_MAX_BYTES = 2048
VIEW_STATE_MAX_KEYS = 16

_APP_ID = re.compile(r'^[a-z][a-z0-9-]{0,31}$')
_SHARED_MODES = ('shared_read', 'shared_write')


class RecentsError(ValueError):
    """A request the caller can fix. The message is shown as-is."""


class NotFound(LookupError):
    """No readable file with that id. Unknown and foreign ids look the same."""


# -- validation ---------------------------------------------------------------

def clean_app(app) -> str:
    if app in (None, ''):
        return ''
    if not isinstance(app, str) or not _APP_ID.match(app):
        raise RecentsError('app must be a short lowercase id such as "docs" or "pdf".')
    return app


def clean_view_state(value) -> dict:
    """A small flat dict of scalars, or a `RecentsError` saying why not."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RecentsError('view_state must be an object.')
    if len(value) > VIEW_STATE_MAX_KEYS:
        raise RecentsError(f'view_state may hold at most {VIEW_STATE_MAX_KEYS} keys.')
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 32:
            raise RecentsError('view_state keys must be short strings.')
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise RecentsError('view_state values must be numbers, strings, booleans or null.')
    if len(json.dumps(value)) > VIEW_STATE_MAX_BYTES:
        raise RecentsError('view_state is too large.')
    return value


def _document_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RecentsError('document_id must be a positive integer.')
    return value


# -- readability --------------------------------------------------------------

def _readable_q(user, prefix: str = '') -> Q:
    """The file-reading predicate, the same one `views._readable_document` uses."""
    return Q(**{f'{prefix}user': user}) | Q(**{f'{prefix}sharing_mode__in': _SHARED_MODES})


def _readable(user, document_id: int) -> Document:
    # `Document.objects` is the live manager, so a trashed file is not found.
    doc = Document.objects.filter(_readable_q(user), id=document_id).first()
    if doc is None:
        raise NotFound(document_id)
    return doc


def _live_readable_rows(user):
    qs = (RecentFile.objects.filter(user=user, document__deleted_at__isnull=True)
          .filter(_readable_q(user, 'document__'))
          .select_related('document', 'document__user', 'document__knowledge_base', 'document__folder'))
    hidden = fs.eval_subtree_ids(user)
    if hidden:
        qs = qs.exclude(document__folder_id__in=hidden)
    return qs


# -- recently opened ----------------------------------------------------------

def record_open(user, document_id, app='', view_state=None) -> RecentFile:
    """Move a file to the top of the user's recents, creating the row if needed.

    `view_state`, when given, replaces what was stored; when absent the stored
    state is kept, so the response tells the opening app where the file was left.
    """
    document_id = _document_id(document_id)
    app = clean_app(app)
    state = clean_view_state(view_state) if view_state is not None else None
    doc = _readable(user, document_id)
    now = timezone.now()
    with transaction.atomic():
        # get_or_create, not filter-then-create: two tabs opening the same file
        # at once would otherwise race into the unique constraint.
        row, created = RecentFile.objects.select_for_update().get_or_create(
            user=user, document=doc,
            defaults={'app': app, 'opened_at': now, 'open_count': 1, 'view_state': state or {}},
        )
        if not created:
            row.opened_at = now
            row.open_count += 1
            if app:
                row.app = app
            if state is not None:
                row.view_state = state
            row.save(update_fields=['opened_at', 'open_count', 'app', 'view_state'])
        _trim(user)
    return row


def save_view_state(user, document_id, view_state) -> RecentFile:
    """Store where the user is in a file without counting it as another open."""
    document_id = _document_id(document_id)
    state = clean_view_state(view_state)
    doc = _readable(user, document_id)
    with transaction.atomic():
        row, created = RecentFile.objects.select_for_update().get_or_create(
            user=user, document=doc,
            defaults={'opened_at': timezone.now(), 'view_state': state},
        )
        if not created:
            row.view_state = state
            row.save(update_fields=['view_state'])
        else:
            _trim(user)
    return row


def get_view_state(user, document_id) -> dict:
    document_id = _document_id(document_id)
    _readable(user, document_id)
    row = RecentFile.objects.filter(user=user, document_id=document_id).only('view_state').first()
    return dict(row.view_state or {}) if row else {}


def listing(user, *, app: str = '', types=None, limit: int = 20) -> list[RecentFile]:
    """The user's recently opened files, newest first, readable ones only.

    `app` narrows to files last opened in that app, and `types` to the given
    `file_type` values. An app lists every file it can open, not only the ones
    it opened itself, which is what `types` is for.
    """
    qs = _live_readable_rows(user)
    if app:
        qs = qs.filter(app=clean_app(app))
    if types:
        qs = qs.filter(document__file_type__in=list(types))
    limit = max(1, min(int(limit), LIST_LIMIT))
    return list(qs.order_by('-opened_at', '-id')[:limit])


def forget(user, document_id=None) -> int:
    """Remove one file from the recents, or clear them all. Returns rows removed."""
    qs = RecentFile.objects.filter(user=user)
    if document_id is not None:
        qs = qs.filter(document_id=_document_id(document_id))
    deleted, _ = qs.delete()
    return deleted


def _trim(user) -> None:
    stale = list(RecentFile.objects.filter(user=user)
                 .order_by('-opened_at', '-id')
                 .values_list('id', flat=True)[RECENT_CAP:])
    if stale:
        RecentFile.objects.filter(id__in=stale).delete()


# -- app sessions (open tabs) --------------------------------------------------

def clean_tabs(tabs) -> list[int]:
    if tabs is None:
        return []
    if not isinstance(tabs, list):
        raise RecentsError('tabs must be a list of document ids.')
    out: list[int] = []
    for item in tabs:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise RecentsError('tabs must be a list of document ids.')
        if item not in out:
            out.append(item)
    return out[:MAX_TABS]


def session(user, app: str) -> dict:
    """An app's saved tabs, resolved to live readable files, in saved order."""
    app = clean_app(app)
    if not app:
        raise RecentsError('app is required.')
    row = AppSession.objects.filter(user=user, app=app).first()
    if row is None:
        return {'app': app, 'tabs': [], 'active': None, 'updated_at': None}
    ids = [i for i in (row.tabs or []) if isinstance(i, int)]
    docs = {d.id: d for d in Document.objects.filter(_readable_q(user), id__in=ids)
            .only('id', 'name', 'file_type')}
    hidden = fs.eval_subtree_ids(user)
    if hidden:
        for d in Document.objects.filter(id__in=list(docs), folder_id__in=hidden).only('id'):
            docs.pop(d.id, None)
    tabs = [{'id': i, 'name': docs[i].name, 'file_type': docs[i].file_type} for i in ids if i in docs]
    active = row.active if row.active in docs else None
    return {'app': app, 'tabs': tabs, 'active': active, 'updated_at': row.updated_at}


def save_session(user, app: str, tabs, active=None) -> dict:
    """Replace an app's saved tabs. Ids the caller cannot read are dropped."""
    app = clean_app(app)
    if not app:
        raise RecentsError('app is required.')
    ids = clean_tabs(tabs)
    if active is not None:
        active = _document_id(active)
    readable = set(Document.objects.filter(_readable_q(user), id__in=ids).values_list('id', flat=True))
    ids = [i for i in ids if i in readable]
    if active not in readable:
        active = None
    AppSession.objects.update_or_create(
        user=user, app=app, defaults={'tabs': ids, 'active': active},
    )
    return session(user, app)
