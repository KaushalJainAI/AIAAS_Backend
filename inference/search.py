"""
Search for the file browser: exact matches first, then close matches by name.

`vfs.find` already answers "which file is called X, or mentions it" for
agents. This is the same question asked by a person typing into a search box,
and a person misspells things — so it runs in two tiers and keeps them apart:

1. **Exact** (the database, names *and* contents). Every word of the query
   must appear, each in the name or in the text; a file whose name holds the
   whole phrase ranks first, then any other name hit, then content-only hits.
   "report quarterly" finds "Quarterly Report Q1.xlsx", which one `icontains`
   on the whole string would not.
2. **Close matches** (Python, names only). Only fills the slots the exact tier
   left empty. Every query word must closely match some word of the name —
   `quaterly repot` → `Quarterly Report` — and the threshold is conservative:
   a near-miss offered as a hit is the failure `vfs.find` warns about, so the
   two tiers are returned separately and the UI labels the second one.

Contents are never fuzzy-matched. That would mean reading up to 500k chars per
row into Python on every keystroke; a typo in a word inside a file is what the
knowledge-base search is for.

Scoping follows the listing endpoint exactly: the caller's live documents,
never the hidden eval tree, never the trash (`LiveManager`), and a folder id
resolved through `filesystem.resolve_folder` so a foreign id is a 404.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Greatest, Lower, StrIndex, Substr

from . import filesystem as fs
from .models import Document, Folder

MIN_QUERY_CHARS = 2
DEFAULT_LIMIT = 30
MAX_LIMIT = 50
MAX_QUERY_CHARS = 200
#: At most this many words are matched; more is a sentence, not a search.
MAX_WORDS = 8
#: How many recent names the close-match tier scores. Names are short, so this
#: is cheap; contents are never loaded here.
FUZZY_CANDIDATES = 2000
FUZZY_THRESHOLD = 0.75
FOLDER_LIMIT = 10
SNIPPET_BEFORE = 60
SNIPPET_CHARS = 160

_WORD = re.compile(r'[a-z0-9]+')

#: "the caller did not narrow to a folder" — distinct from None, the root.
EVERYWHERE = object()


def normalise(text: str) -> str:
    """Lower-case, NFKC and accents stripped, so `Café` finds `cafe`."""
    text = unicodedata.normalize('NFKD', unicodedata.normalize('NFKC', text or ''))
    return ''.join(c for c in text if not unicodedata.combining(c)).lower()


def words(text: str) -> list[str]:
    return _WORD.findall(normalise(text))


def _stem(name: str) -> str:
    """The name without its extension: `.xlsx` is not something anyone misspells."""
    base, dot, ext = (name or '').rpartition('.')
    return base if dot and base and len(ext) <= 5 else (name or '')


def word_score(q: str, w: str) -> float:
    """How closely one query word matches one name word, 0..1.

    A prefix is a full match (`quart` → `quarterly`, as-you-type). Otherwise a
    `SequenceMatcher` ratio, but only between words whose lengths are close and
    that share a first letter or a length — which is what keeps it
    conservative: it catches `quaterly`, `repot`, `raport`, and drops the
    loose matches between unrelated short words.
    """
    if not q or not w:
        return 0.0
    if w.startswith(q) and len(q) >= 2:
        return 1.0
    if len(q) < 3 or abs(len(q) - len(w)) > max(2, len(q) // 3):
        return 0.0
    if q[0] != w[0] and len(q) != len(w):
        return 0.0
    return SequenceMatcher(None, q, w, autojunk=False).ratio()


def fuzzy_score(query_words: list[str], name: str, *, has_extension: bool = True) -> float:
    """Every query word must clear the threshold against some word of the
    name; the score is the mean. 0 when any word has no match."""
    name_words = words(_stem(name) if has_extension else name)
    if not query_words or not name_words:
        return 0.0
    total = 0.0
    for q in query_words:
        best = max(word_score(q, w) for w in name_words)
        if best < FUZZY_THRESHOLD:
            return 0.0
        total += best
    return total / len(query_words)


def _clamp_limit(raw) -> int:
    try:
        return min(max(int(raw), 1), MAX_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _scoped_documents(user, folder, scope: str, types):
    if scope == 'public':
        # The shared library is flat and readable by every signed-in user —
        # the listing's `_shared_documents`. No folders, no tree.
        rows = Document.objects.filter(sharing_mode__in=['shared_read', 'shared_write'])
    else:
        rows = Document.objects.filter(user=user)
        hidden = fs.eval_subtree_ids(user)
        if hidden:
            rows = rows.exclude(folder_id__in=hidden)
        if folder is not EVERYWHERE and folder is not None:
            rows = rows.filter(folder__in=fs.subtree(folder, include_trashed=False))
        # folder None (root) is the whole tree: every file is beneath the root.
    if types:
        rows = rows.filter(file_type__in=types)
    return rows


def _scoped_folders(user, folder):
    # The queryset itself is built in `filesystem.folders_beneath`: the
    # choke-point rule keeps folder-manager access inside that module
    # (`ChokePointTests`), and this only narrows what it returns.
    if folder is EVERYWHERE or folder is None:
        return fs.folders_beneath(user, None)
    return fs.folders_beneath(user, folder)


def _snippets(ids: list[int], needle: str) -> dict[int, str]:
    """A short window of text around the first occurrence of `needle`, cut in
    the database so a 500k-char document is never loaded to show 160 chars.

    `Lower` in SQLite folds ASCII only; a miss there just means no snippet.
    """
    if not ids or not needle:
        return {}
    pos = StrIndex(Lower('content_text'), Value(needle.lower()))
    start = Greatest(pos - SNIPPET_BEFORE, Value(1))
    rows = (Document.objects.filter(pk__in=ids)
            .annotate(_pos=pos, _start=start,
                      _snip=Substr('content_text', start, SNIPPET_CHARS))
            .values_list('pk', '_pos', '_start', '_snip'))
    out = {}
    for pk, found_at, start, snip in rows:
        if not found_at or not snip:
            continue
        text = ' '.join(str(snip).split())
        if start > 1:
            text = '…' + text
        # Only mark a cut that happened: a window that reached the end of the
        # text is the whole rest of it.
        if len(snip) >= SNIPPET_CHARS:
            text += '…'
        out[pk] = text
    return out


def search(user, query: str, *, folder=EVERYWHERE, scope: str = 'personal',
           types=None, limit=DEFAULT_LIMIT) -> dict:
    """Search `user`'s files. Returns plain data: document rows, not dicts —
    the view serializes them with the listing serializer."""
    needle = ' '.join((query or '').split())[:MAX_QUERY_CHARS]
    cap = _clamp_limit(limit)
    empty = {'query': needle, 'exact': [], 'fuzzy': [], 'folders': [],
             'matched_in': {}, 'snippets': {}, 'scores': {},
             'truncated': False}
    if len(needle) < MIN_QUERY_CHARS:
        return empty

    terms = needle.split()[:MAX_WORDS]
    base = _scoped_documents(user, folder, scope, types)

    # ---- Tier 1: every word, in the name or the text ------------------------
    exact_q = Q()
    for term in terms:
        exact_q &= Q(name__icontains=term) | Q(content_text__icontains=term)
    name_all = Q()
    for term in terms:
        name_all &= Q(name__icontains=term)
    rank = Case(
        When(name__icontains=needle, then=Value(0)),
        When(name_all, then=Value(1)),
        default=Value(2), output_field=IntegerField(),
    )
    exact_rows = list(
        base.filter(exact_q).annotate(_rank=rank)
        .select_related('user', 'knowledge_base', 'folder')
        .defer('content_text')
        .order_by('_rank', '-updated_at')[:cap + 1]
    )
    truncated = len(exact_rows) > cap
    exact_rows = exact_rows[:cap]
    matched_in = {d.pk: ('name' if d._rank < 2 else 'content') for d in exact_rows}

    # A snippet for content-only hits, around the first word that is not in
    # the name (the one that made it a content match).
    content_ids = [d.pk for d in exact_rows if matched_in[d.pk] == 'content']
    snippets: dict[int, str] = {}
    if content_ids:
        lowered = {d.pk: d.name.lower() for d in exact_rows}
        by_term: dict[str, list[int]] = {}
        for pk in content_ids:
            term = next((t for t in terms if t.lower() not in lowered[pk]), terms[0])
            by_term.setdefault(term, []).append(pk)
        for term, ids in by_term.items():
            snippets.update(_snippets(ids, needle if len(terms) == 1 else term))

    # ---- Tier 2: close matches by name, filling what is left -----------------
    fuzzy_rows: list[Document] = []
    scores: dict[int, float] = {}
    room = cap - len(exact_rows)
    query_words = words(needle)[:MAX_WORDS]
    if room > 0 and query_words and not truncated:
        seen = {d.pk for d in exact_rows}
        candidates = (base.exclude(pk__in=seen)
                      .order_by('-updated_at')
                      .values_list('pk', 'name')[:FUZZY_CANDIDATES])
        scored = [(fuzzy_score(query_words, name), pk) for pk, name in candidates]
        scored = [(s, pk) for s, pk in scored if s > 0]
        # Stable on recency (the candidate order), best score first.
        scored.sort(key=lambda sp: -sp[0])
        keep = scored[:room]
        if keep:
            scores = {pk: round(s, 2) for s, pk in keep}
            by_pk = {d.pk: d for d in (
                Document.objects.filter(pk__in=scores)
                .select_related('user', 'knowledge_base', 'folder')
                .defer('content_text'))}
            fuzzy_rows = [by_pk[pk] for _, pk in keep if pk in by_pk]
            for d in fuzzy_rows:
                matched_in[d.pk] = 'fuzzy'

    # ---- Folders, by name — a misspelled folder should still navigate -------
    folders: list[tuple[Folder, str]] = []
    if scope != 'public':
        fq = _scoped_folders(user, folder)
        name_q = Q()
        for term in terms:
            name_q &= Q(name__icontains=term)
        exact_folders = list(fq.filter(name_q).order_by('name')[:FOLDER_LIMIT])
        folders = [(f, 'name') for f in exact_folders]
        if len(folders) < FOLDER_LIMIT and query_words:
            seen_f = {f.pk for f in exact_folders}
            close = []
            for f in fq.exclude(pk__in=seen_f).order_by('-updated_at')[:FUZZY_CANDIDATES]:
                s = fuzzy_score(query_words, f.name, has_extension=False)
                if s > 0:
                    close.append((s, f))
            close.sort(key=lambda sf: -sf[0])
            folders += [(f, 'fuzzy') for _, f in close[:FOLDER_LIMIT - len(folders)]]

    return {
        'query': needle,
        'exact': exact_rows,
        'fuzzy': fuzzy_rows,
        'folders': folders,
        'matched_in': matched_in,
        'snippets': snippets,
        'scores': scores,
        'truncated': truncated,
    }
