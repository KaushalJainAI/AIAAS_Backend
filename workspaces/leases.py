"""
File leases: writing takes a lock.

Only writes lock — readers are never blocked. A lease is a normalised path or
a `dir/**` subtree pattern held by one `ExecutionLog`. Overlap is computed on
glob prefixes: `a/**` overlaps `a/b.ts`, and two exact paths overlap only if
equal. `*` and `?` match within one segment; `**` crosses segments.

Enforced in code under `select_for_update` on the project row, because overlap
is not equality and no DB constraint can express it. Expired leases
(`expires_at` past) are treated as absent and reaped on the next acquire.
"""
from __future__ import annotations

import fnmatch
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Heartbeat TTL: a lease lives 10 min past the holder's last tool call.
LEASE_TTL = timedelta(minutes=10)


def normalize_pattern(raw: str) -> str:
    """Normalise a claim or path the way `vfs` clamps: strip leading `/`,
    drop `.` and empty segments, clamp `..` at the root.
    """
    pattern = str(raw or '').strip().lstrip('/')
    kept: list[str] = []
    for seg in pattern.split('/'):
        if seg in ('', '.'):
            continue
        if seg == '..':
            if kept:
                kept.pop()
            continue
        kept.append(seg)
    return '/'.join(kept)


def normalize_path(raw: str) -> str:
    """A concrete file path normalises exactly like a pattern."""
    return normalize_pattern(raw)


def _segments(pattern: str) -> list[str]:
    return pattern.split('/') if pattern else []


def overlaps(a: str, b: str) -> bool:
    """Whether two patterns can cover the same file.

    Exact paths overlap only when equal. A pattern with a glob overlaps another
    when one side's literal prefix admits the other — computed conservatively:
    if either side has a glob character in the segments where they differ, they
    overlap, because ruling "no overlap" wrongly lets two writers collide.
    """
    a, b = normalize_pattern(a), normalize_pattern(b)
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = _segments(a), _segments(b)
    common = min(len(sa), len(sb))
    for i in range(common):
        x, y = sa[i], sb[i]
        if x == '**' or y == '**':
            return True
        if x == y:
            continue
        # One side is a prefix glob of the other (`src/**` vs `src/api/x.ts`):
        # the shorter side ending here means containment.
        if _has_glob(x) or _has_glob(y):
            # Compare segment-wise with fnmatch; a mismatch here still overlaps
            # when the remaining tail could match (conservative).
            if fnmatch.fnmatchcase(y, x) or fnmatch.fnmatchcase(x, y):
                continue
            return True
        return False
    # All shared segments matched: the shorter side contains the longer one
    # when it ends in `**`, or when it is a directory prefix.
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if shorter and shorter[-1] == '**':
        return True
    # `src/api` claims the subtree the same way `src/api/**` does.
    if len(sa) != len(sb):
        return True
    return False


def _has_glob(seg: str) -> bool:
    return '*' in seg or '?' in seg or '[' in seg


def subsumes(super_pattern: str, sub_pattern: str) -> bool:
    """Whether every path `sub_pattern` can cover is also covered by
    `super_pattern` — answered conservatively.

    When in doubt this returns False (the pattern is dropped from the
    intersection, narrowing the worker). A wrong True would widen a worker
    past its parent, which is the outcome the intersection exists to prevent;
    a wrong False only ever refuses a write the lead can re-scope.
    """
    sup, sub = normalize_pattern(super_pattern), normalize_pattern(sub_pattern)
    if not sup or not sub:
        return False
    if sup == sub:
        return True
    if sup == '**':
        return True
    if sup.endswith('/**'):
        prefix = sup[:-3]
        if sub == prefix or sub.startswith(prefix + '/'):
            return True
        # `src/api/**` is under `src/**` even though the strings differ.
        sub_segs, pre_segs = _segments(sub), _segments(prefix)
        if len(sub_segs) >= len(pre_segs) and not any(
            _has_glob(s) for s in sub_segs[:len(pre_segs)]
        ):
            if sub_segs[:len(pre_segs)] == pre_segs:
                return True
        return False
    if not _has_glob(sup) and not _has_glob(sub):
        return sub == sup or sub.startswith(sup + '/')
    return False


def covers(pattern: str, path: str) -> bool:
    """Whether lease `pattern` covers concrete file `path`."""
    pattern, path = normalize_pattern(pattern), normalize_path(path)
    if not pattern or not path:
        return False
    if pattern == path:
        return True
    if pattern.endswith('/**'):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + '/')
    if _has_glob(pattern):
        return fnmatch.fnmatchcase(path, pattern)
    # A bare directory pattern covers its subtree.
    return path == pattern or path.startswith(pattern + '/')


def _prune_expired(project_id: int) -> int:
    """Delete expired leases for one project. Returns the count removed."""
    from workspaces.models import CodeLease

    now = timezone.now()
    deleted, _ = CodeLease.objects.filter(
        project_id=project_id, expires_at__lt=now).delete()
    return deleted


def live_leases(project_id: int):
    """Live (unexpired) leases for one project, oldest first."""
    from workspaces.models import CodeLease

    _prune_expired(project_id)
    return list(CodeLease.objects.filter(project_id=project_id).order_by('acquired_at'))


def acquire(project, holder, patterns, holder_label: str = '', task_id: str = ''):
    """Lease every pattern in `patterns` atomically for `holder`.

    Raises `LeaseConflict` naming the holder when any pattern overlaps a live
    lease someone else holds. Idempotent for the holder's own patterns.
    """
    from workspaces.models import CodeLease, CodeProject

    cleaned = [normalize_pattern(p) for p in patterns or []]
    cleaned = [p for p in cleaned if p]
    if not cleaned:
        return []

    with transaction.atomic():
        # Serialise lease decisions per project: overlap is not a unique
        # column, so the check-and-insert must hold the project row.
        CodeProject.objects.select_for_update().filter(id=project.id).first()
        _prune_expired(project.id)
        live = list(CodeLease.objects.filter(project_id=project.id))
        for pattern in cleaned:
            for lease in live:
                if lease.holder_id == holder.id:
                    continue
                if overlaps(lease.pattern, pattern):
                    raise LeaseConflict(
                        f'Claims {pattern} overlap {lease.holder_label or "run %s" % lease.holder_id}'
                        f"'s lease on {lease.pattern}"
                        + (f' (task {lease.task_id})' if lease.task_id else '')
                        + '; wait for it or re-scope.'
                    )
        now = timezone.now()
        made = []
        existing = {(l.pattern, l.holder_id) for l in live}
        for pattern in cleaned:
            if (pattern, holder.id) in existing:
                row = next(l for l in live
                           if l.pattern == pattern and l.holder_id == holder.id)
                row.holder_label = holder_label or row.holder_label
                row.task_id = task_id or row.task_id
                row.expires_at = now + LEASE_TTL
                row.save(update_fields=['holder_label', 'task_id', 'expires_at', 'heartbeat_at'])
                made.append(row)
                continue
            made.append(CodeLease.objects.create(
                project=project, pattern=pattern, holder=holder,
                holder_label=holder_label, task_id=task_id,
                expires_at=now + LEASE_TTL,
            ))
        return made


def heartbeat(holder_id: int) -> None:
    """Renew every lease one run holds. Called on each tool call by the holder."""
    from workspaces.models import CodeLease

    try:
        CodeLease.objects.filter(holder_id=holder_id).update(
            heartbeat_at=timezone.now(), expires_at=timezone.now() + LEASE_TTL)
    except Exception:  # noqa: BLE001
        logger.warning('[Leases] Heartbeat failed for run %s', holder_id)


def release_holder(holder_id: int) -> int:
    """Release every lease one run holds. Returns the count released."""
    from workspaces.models import CodeLease

    deleted, _ = CodeLease.objects.filter(holder_id=holder_id).delete()
    if deleted:
        logger.info('[Leases] Released %s lease(s) for run %s', deleted, holder_id)
    return deleted


def release_pattern(project_id: int, pattern: str, holder_id=None) -> int:
    """Release one pattern (the panel's "Release lock"). Releasing does not
    revert the change — it only lets others write."""
    from workspaces.models import CodeLease

    qs = CodeLease.objects.filter(project_id=project_id,
                                  pattern=normalize_pattern(pattern))
    if holder_id is not None:
        qs = qs.filter(holder_id=holder_id)
    deleted, _ = qs.delete()
    return deleted


class LeaseConflict(Exception):
    """A claim overlaps a live lease someone else holds."""
