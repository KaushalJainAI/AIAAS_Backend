"""
Live catalogue refresh: `opencode models --refresh` for this platform.

`populate_models.py` is a hand-curated seed — it knows prices, capabilities
and effort rungs no API reports, but it only changes when someone edits the
file. This module diffs the live OpenRouter `/v1/models` catalogue against
the `openrouter` rows we hold:

- seen live + row held → refresh pricing / context window / `last_seen_at`
  (caps and effort stay hand-curated — no API reports those faithfully);
- seen live + no row → create `is_active=False, source='live'`, reported as
  `new_upstream` for staff to activate in admin (a human gate, so one upstream
  dump cannot flood every picker with 300 ids);
- held (`curated`/`live`) + missing live → `is_active=False, retired_at=now`
  (never deleted — a saved run or agent may still reference the id);
- `hand` rows are never touched by either direction;
- `populate_models.RETIRED_MODEL_VALUES` always wins over live.

Retiring ids then fans out: agents still configured on a retired id are
grouped by owner and each owner gets one `system` notification
(`data.kind='model_retired'`), rate-limited by `ModelFallbackNotice` so one
refresh notifies once no matter how often it runs.

Only the `openrouter` provider is diffed. NIM/OpenAI/Ollama ids are
vendor-named and unverified (per the seed's own comments); refreshing those
against the wrong endpoint would retire rows on bad evidence.

Runs on demand — staff button → endpoint, or host cron → `manage.py
refresh_models` — never at boot. A live fetch in the boot path would delay
every restart and could fail it when OpenRouter is down.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = 'https://openrouter.ai/api/v1/models'

#: Held while a refresh is in flight so two staff clicks (or cron + a click)
#: cannot diff against each other. Losers get 409, like Imagine's
#: `openrouter_capabilities_refreshing` lock.
REFRESH_LOCK_KEY = 'llm_models_refreshing'
REFRESH_LOCK_TTL = 180

#: Last completed refresh, for the `meta` on `GET /api/llm/models/`.
REFRESH_STATUS_KEY = 'llm_models_last_refresh'

#: Only this provider is diffed against live (see module docstring).
REFRESHED_PROVIDER = 'openrouter'

#: Suffixes that are billing modes or aliases, not models — the seed carries
#: none of these, and auto-creating them would multiply rows without adding
#: choice.
VARIANT_SUFFIXES = (':free', ':batch', ':nitro', ':online', ':floor')

#: Known renames, for the `replaced_by` hint. A hint only — nothing applies it
#: automatically, and staff can edit it in admin.
SUGGESTED_SUCCESSORS = {
    'qwen/qwen3.8-max': 'qwen/qwen3.8-max-0902',
    'qwen/qwen3.8-2.4t-a95b': 'qwen/qwen3.8-max-0902',
    'inception/mercury-2.5-preview': 'inception/mercury-2.5',
    'deepseek/deepseek-v4-pro': 'deepseek/deepseek-v4.1-flash',
    'deepseek/deepseek-v4-flash': 'deepseek/deepseek-v4-flash-0731',
    'deepseek/deepseek-v4-pro-0813': 'deepseek/deepseek-v4.1-flash',
    'google/gemini-3.6-flash': 'google/gemini-3.7-flash',
    'google/gemini-3.5-flash-lite': 'google/gemini-3.7-flash',
}


class RefreshError(RuntimeError):
    """The refresh could not run (no key, provider down, bad payload)."""


class RefreshInProgress(RuntimeError):
    """Another refresh holds the lock."""


def _per_million(value) -> Decimal:
    """OpenRouter per-token price string → USD per 1M, 4dp like the columns."""
    try:
        return (Decimal(str(value or '0')) * Decimal(1_000_000)).quantize(
            Decimal('0.0000'))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal('0.0000')


def _caps_for(entry: dict) -> dict:
    """Conservative capability flags for a live entry.

    Only what the listing actually evidences. Anything unclaimed stays off —
    an invented capability is a run that fails at the first token.
    """
    arch = entry.get('architecture') or {}
    modalities = set(arch.get('input_modalities') or [])
    params = set(entry.get('supported_parameters') or [])
    return {
        'supports_text_input': True,
        'supports_text_generation': True,
        'supports_image_input': 'image' in modalities,
        'supports_video_input': 'video' in modalities,
        'supports_audio_input': 'audio' in modalities,
        'supports_document_input': 'file' in modalities,
        'supports_tool_calling': bool(params & {'tools', 'tool_choice'}),
        'supports_structured_output': bool(
            params & {'structured_outputs', 'response_format'}),
    }


def _parse_entry(entry: dict) -> dict | None:
    """One live `data[]` entry → row fields, or None to skip it."""
    value = (entry.get('id') or '').strip()
    if not value or value.endswith(VARIANT_SUFFIXES) or '~' in value:
        return None
    pricing = entry.get('pricing') or {}
    prompt = _per_million(pricing.get('prompt'))
    completion = _per_million(pricing.get('completion'))
    try:
        context = int(entry.get('context_length') or 0)
    except (TypeError, ValueError):
        context = 0
    return {
        'value': value,
        'name': (entry.get('name') or value).strip()[:100],
        'description': '',
        'is_free': prompt == 0 and completion == 0,
        'caps': _caps_for(entry),
        'input_price_per_million': prompt,
        'output_price_per_million': completion,
        'cached_input_price_per_million': (
            _per_million(pricing.get('input_cache_read'))
            if pricing.get('input_cache_read') not in (None, '') else None
        ),
        'context_window': context,
    }


def _fetch_live(api_key: str) -> list[dict]:
    import requests

    try:
        response = requests.get(
            OPENROUTER_MODELS_URL,
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise RefreshError(f'OpenRouter catalogue unreachable: {exc}') from exc
    data = payload.get('data')
    if not isinstance(data, list) or not data:
        # Never blank the table on a transient outage — the Imagine rule.
        raise RefreshError('OpenRouter returned an empty model list; not applying')
    return data


def _resolve_key(user=None) -> str | None:
    from credentials.resolution import (
        CredentialUnavailable,
        platform_api_key,
        resolve_api_key_sync,
    )

    if key := platform_api_key('openrouter'):
        return key
    if user is not None:
        try:
            return resolve_api_key_sync(
                'openrouter', user.id, allow_platform_fallback=False)
        except CredentialUnavailable:
            return None
    return None


def _suggest_successor(value: str, active_values: set[str]) -> str:
    """Best-guess replacement for a retired id. Hint only."""
    if value in SUGGESTED_SUCCESSORS:
        hint = SUGGESTED_SUCCESSORS[value]
        if hint in active_values:
            return hint
    # Same namespace, and the retired id is a prefix of the candidate
    # (`qwen/qwen3.8-max` → `qwen/qwen3.8-max-0902`). Shortest wins: the base
    # snapshot, not a dated one of it.
    namespace = value.split('/')[0] + '/'
    cands = sorted(
        v for v in active_values
        if v.startswith(namespace) and v != value and v.startswith(value)
    )
    return cands[0] if cands else ''


def refresh_catalog(*, user=None) -> dict:
    """Diff live OpenRouter against held rows. See module docstring.

    Returns {added, updated, retired, new_upstream, affected_agents}.
    Raises `RefreshInProgress` when another refresh holds the lock,
    `RefreshError` when it cannot run (nothing is written then).
    """
    from llm.models import AIModel, AIProvider

    if not cache.add(REFRESH_LOCK_KEY, True, timeout=REFRESH_LOCK_TTL):
        raise RefreshInProgress('A catalogue refresh is already running')
    try:
        api_key = _resolve_key(user)
        if not api_key:
            raise RefreshError(
                'No OpenRouter key: set the OPENROUTER_API_KEY platform key '
                "or add your own 'OpenRouter API' credential under Credentials."
            )
        live = [_parse_entry(e) for e in _fetch_live(api_key)]
        live = [e for e in live if e]
        if not live:
            raise RefreshError('OpenRouter returned no usable models; not applying')
        with transaction.atomic():
            summary = _apply(live)
        _notify_affected(summary)
        status = {
            'at': timezone.now().isoformat(),
            'by': getattr(user, 'username', None) or 'cron',
            **{k: v for k, v in summary.items() if k != 'retired_values'},
        }
        cache.set(REFRESH_STATUS_KEY, status, timeout=None)
        return summary
    finally:
        cache.delete(REFRESH_LOCK_KEY)


def _apply(live: list[dict]) -> dict:
    """The diff itself, inside one transaction. Returns the summary."""
    from llm.models import AIModel, AIProvider
    from populate_models import RETIRED_MODEL_VALUES

    now = timezone.now()
    provider = AIProvider.objects.filter(
        slug=REFRESHED_PROVIDER, is_active=True).first()
    if provider is None:
        raise RefreshError(
            f"No active '{REFRESHED_PROVIDER}' provider row — run the seed "
            'first (it runs at every backend boot).'
        )
    live_by_value = {e['value']: e for e in live}
    held = AIModel.objects.filter(provider=provider)
    held_by_value = {m.value: m for m in held}

    added, updated, retired, new_upstream = 0, 0, [], []

    for value, entry in live_by_value.items():
        row = held_by_value.get(value)
        if row is None:
            # Discovered upstream, gated by staff: visible in admin, never
            # offered until someone flips it on. Auto-offering every upstream
            # id would put 300 models in every picker.
            AIModel.objects.create(
                provider=provider, name=entry['name'], value=value,
                description=entry['description'], is_active=False,
                is_free=entry['is_free'], source='live',
                input_price_per_million=entry['input_price_per_million'],
                output_price_per_million=entry['output_price_per_million'],
                cached_input_price_per_million=entry[
                    'cached_input_price_per_million'],
                context_window=entry['context_window'],
                last_seen_at=now, **entry['caps'],
            )
            added += 1
            new_upstream.append(value)
            continue
        changed = False
        for field in ('input_price_per_million', 'output_price_per_million',
                      'cached_input_price_per_million', 'context_window'):
            if getattr(row, field) != entry[field]:
                setattr(row, field, entry[field])
                changed = True
        if entry['is_free'] != row.is_free:
            row.is_free = entry['is_free']
            changed = True
        row.last_seen_at = now
        if row.retired_at is not None or not row.is_active:
            # Back upstream (or staff reactivated it while it was listed):
            # re-list, clear the retirement.
            row.is_active, row.retired_at = True, None
            changed = True
        # An explicit staff successor stands; otherwise refresh the hint.
        if not row.replaced_by:
            row.replaced_by = ''
        if changed:
            row.save()
            updated += 1
        else:
            row.save(update_fields=['last_seen_at'])

    live_values = set(live_by_value)
    active_values = set(live_values)
    # Missing live + held as curated/live = retired. `hand` rows are spared:
    # a person put them there, and live has no standing to remove them.
    missing = held.filter(is_active=True).exclude(value__in=live_values)
    missing = missing.exclude(source='hand')
    forced = held.filter(value__in=RETIRED_MODEL_VALUES, is_active=True)
    to_retire = {m.value: m for m in list(missing) + list(forced)}
    for value, row in to_retire.items():
        row.is_active, row.retired_at = False, now
        hint = _suggest_successor(value, active_values)
        if hint and not row.replaced_by:
            row.replaced_by = hint
        row.save(update_fields=['is_active', 'retired_at', 'replaced_by'])
        retired.append({'value': value, 'replaced_by': row.replaced_by})

    return {
        'added': added,
        'updated': updated,
        'retired': retired,
        'new_upstream': sorted(new_upstream),
        'affected_agents': _affected_agents([r['value'] for r in retired]),
        'retired_values': [r['value'] for r in retired],
    }


def _affected_agents(retired_values: list[str]) -> list[dict]:
    """Agents still configured on a retired id (id, name, owner, successor)."""
    if not retired_values:
        return []
    from agents.models import SubAgent
    from llm.models import AIModel

    hints = dict(AIModel.objects.filter(value__in=retired_values).values_list(
        'value', 'replaced_by'))
    rows = (SubAgent.objects.filter(llm_model__in=retired_values)
            .values('id', 'name', 'user_id', 'llm_model'))
    return [{
        'id': r['id'], 'name': r['name'], 'user_id': r['user_id'],
        'old': r['llm_model'], 'suggested': hints.get(r['llm_model']) or '',
    } for r in rows]


def _notify_affected(summary: dict) -> None:
    """One notification per owner of affected agents. Best-effort."""
    agents = summary.get('affected_agents') or []
    if not agents:
        return
    from django.contrib.auth import get_user_model

    from llm.models import ModelFallbackNotice
    from notifications.utils import create_notification

    by_user: dict[int, list[dict]] = {}
    for agent in agents:
        _, created = ModelFallbackNotice.objects.get_or_create(
            subagent_id=agent['id'], old_value=agent['old'])
        if created:
            by_user.setdefault(agent['user_id'], []).append(agent)
    User = get_user_model()
    for user_id, owned in by_user.items():
        try:
            owner = User.objects.filter(pk=user_id).first()
            if owner is None:
                continue
            lines = []
            for agent in owned[:10]:
                line = (f"• {agent['name']}: `{agent['old']}` was retired"
                        + (f" — suggested: `{agent['suggested']}`"
                           if agent['suggested'] else
                           " — pick a replacement in the builder"))
                lines.append(line)
            if len(owned) > 10:
                lines.append(f"• …and {len(owned) - 10} more")
            lines.append(
                "\nRuns on these agents now use the platform fallback model "
                "until you choose a replacement; nothing was rewritten.")
            create_notification(
                owner, 'system',
                f"{len(owned)} of your agents' models were retired",
                '\n'.join(lines),
                data={
                    'kind': 'model_retired',
                    'agents': [
                        {'id': a['id'], 'old': a['old'],
                         'suggested': a['suggested']} for a in owned
                    ],
                    'action_url': '/agents',
                },
                send_email=False,
            )
        except Exception:  # noqa: BLE001 — telling must not fail refreshing
            logger.warning('[Refresh] Notify failed for user %s', user_id,
                           exc_info=True)
